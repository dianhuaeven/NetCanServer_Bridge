#include "bridge.hpp"

#include <algorithm>
#include <arpa/inet.h>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

namespace {

bool set_non_blocking(int fd) {
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return false;
    }
    if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        return false;
    }
    return true;
}

void close_fd(int &fd) {
    if (fd >= 0) {
        close(fd);
        fd = -1;
    }
}

} // namespace

BridgeApp::BridgeApp(const BridgeConfig &config)
    : config_(config),
      epoll_fd_(-1) {
    tx_buffer_.fill(0);
}

BridgeApp::~BridgeApp() {
    shutdown();
}

bool BridgeApp::initialize() {
    shutdown();

    if (config_.ports.empty()) {
        std::fprintf(stderr, "configuration must contain at least one UDP port\n");
        return false;
    }

    in_addr server_addr{};
    if (inet_pton(AF_INET, config_.server.ip.c_str(), &server_addr) != 1) {
        std::fprintf(stderr, "invalid server ip address: %s\n", config_.server.ip.c_str());
        return false;
    }

    epoll_fd_ = epoll_create1(0);
    if (epoll_fd_ < 0) {
        std::perror("failed to create epoll instance");
        return false;
    }

    std::size_t total_channels = 0;
    for (const auto &port : config_.ports) {
        total_channels += port.channels.size();
    }

    udp_ports_.clear();
    channels_.clear();
    id_lookup_.clear();
    udp_ports_.reserve(config_.ports.size());
    channels_.reserve(total_channels);
    id_lookup_.reserve(total_channels);

    for (const auto &port_cfg : config_.ports) {
        UdpPortContext port_ctx{};
        port_ctx.config = port_cfg;
        port_ctx.remote_addr.sin_family = AF_INET;
        port_ctx.remote_addr.sin_addr = server_addr;
        port_ctx.remote_addr.sin_port = htons(port_cfg.send_port);
        port_ctx.rx_buffer.fill(0);

        if (!configure_udp_socket(port_ctx)) {
            shutdown();
            return false;
        }

        udp_ports_.push_back(std::move(port_ctx));
        const std::size_t port_index = udp_ports_.size() - 1;

        if (!register_event(EventType::Udp, static_cast<std::uint32_t>(port_index), udp_ports_[port_index].udp_fd)) {
            shutdown();
            return false;
        }

        std::printf("[UDP:%zu] listen 0.0.0.0:%u -> %s:%u\n",
                    port_index,
                    udp_ports_[port_index].config.listen_port,
                    config_.server.ip.c_str(),
                    udp_ports_[port_index].config.send_port);

        for (const auto &channel_cfg : port_cfg.channels) {
            ChannelContext channel_ctx{};
            channel_ctx.config = channel_cfg;
            channel_ctx.port_index = port_index;

            if (!configure_can_socket(channel_ctx)) {
                shutdown();
                return false;
            }

            channels_.push_back(std::move(channel_ctx));
            const std::size_t channel_index = channels_.size() - 1;

            if (!register_event(EventType::Can, static_cast<std::uint32_t>(channel_index), channels_[channel_index].can_fd)) {
                shutdown();
                return false;
            }

            RangeLookup lookup{};
            lookup.range = channel_cfg.id_range;
            lookup.channel_index = channel_index;
            id_lookup_.push_back(lookup);

            std::printf("[CAN:%zu] %s range[0x%08X,0x%08X] -> UDP port %zu\n",
                        channel_index,
                        channels_[channel_index].config.vcan_name.c_str(),
                        channels_[channel_index].config.id_range.min,
                        channels_[channel_index].config.id_range.max,
                        channels_[channel_index].port_index);
        }
    }

    std::sort(id_lookup_.begin(), id_lookup_.end(),
              [](const RangeLookup &lhs, const RangeLookup &rhs) { return lhs.range.min < rhs.range.min; });

    return true;
}

void BridgeApp::run(std::atomic<bool> &keep_running) {
    if (epoll_fd_ < 0) {
        return;
    }

    const std::size_t max_events = udp_ports_.size() + channels_.size();
    if (max_events == 0) {
        return;
    }

    std::vector<epoll_event> events(max_events);
    while (keep_running.load()) {
        const int ready = epoll_wait(epoll_fd_, events.data(), static_cast<int>(events.size()), 1000);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            std::perror("epoll_wait failed");
            break;
        }
        if (ready == 0) {
            continue;
        }

        for (int i = 0; i < ready; ++i) {
            const EventType type = decode_event_type(events[i].data.u64);
            const std::uint32_t index = decode_event_index(events[i].data.u64);
            switch (type) {
            case EventType::Udp:
                if (index < udp_ports_.size()) {
                    handle_udp_events(index);
                }
                break;
            case EventType::Can:
                if (index < channels_.size()) {
                    handle_can_events(index);
                }
                break;
            default:
                break;
            }
        }
    }
}

bool BridgeApp::configure_udp_socket(UdpPortContext &context) {
    context.udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (context.udp_fd < 0) {
        std::perror("failed to create UDP socket");
        return false;
    }
    if (!set_non_blocking(context.udp_fd)) {
        std::perror("failed to set UDP non-blocking");
        close_fd(context.udp_fd);
        return false;
    }

    int opt = 1;
    if (setsockopt(context.udp_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        std::perror("setsockopt SO_REUSEADDR failed");
    }

    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = INADDR_ANY;
    local.sin_port = htons(context.config.listen_port);
    if (bind(context.udp_fd, reinterpret_cast<sockaddr *>(&local), sizeof(local)) < 0) {
        std::perror("failed to bind UDP socket");
        close_fd(context.udp_fd);
        return false;
    }

    return true;
}

bool BridgeApp::configure_can_socket(ChannelContext &context) {
    context.can_fd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
    if (context.can_fd < 0) {
        std::perror("failed to create CAN socket");
        return false;
    }
    if (!set_non_blocking(context.can_fd)) {
        std::perror("failed to set CAN non-blocking");
        close_fd(context.can_fd);
        return false;
    }

    ifreq ifr{};
    std::strncpy(ifr.ifr_name, context.config.vcan_name.c_str(), sizeof(ifr.ifr_name));
    ifr.ifr_name[sizeof(ifr.ifr_name) - 1] = '\0';
    if (ioctl(context.can_fd, SIOCGIFINDEX, &ifr) < 0) {
        std::perror("failed to lookup CAN interface");
        close_fd(context.can_fd);
        return false;
    }

    sockaddr_can addr{};
    addr.can_family = AF_CAN;
    addr.can_ifindex = ifr.ifr_ifindex;
    if (bind(context.can_fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        std::perror("failed to bind CAN socket");
        close_fd(context.can_fd);
        return false;
    }

    return true;
}

bool BridgeApp::register_event(EventType type, std::uint32_t index, int fd) {
    if (epoll_fd_ < 0 || fd < 0) {
        return false;
    }
    epoll_event event{};
    event.events = EPOLLIN;
    event.data.u64 = make_event_tag(type, index);
    if (epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, fd, &event) < 0) {
        std::perror("failed to register fd with epoll");
        return false;
    }
    return true;
}

void BridgeApp::shutdown() {
    for (auto &channel : channels_) {
        close_fd(channel.can_fd);
    }
    for (auto &port : udp_ports_) {
        close_fd(port.udp_fd);
    }
    close_fd(epoll_fd_);
}

void BridgeApp::handle_udp_events(std::size_t port_index) {
    if (port_index >= udp_ports_.size()) {
        return;
    }

    UdpPortContext &port = udp_ports_[port_index];
    while (true) {
        const ssize_t received = recv(port.udp_fd, port.rx_buffer.data(), port.rx_buffer.size(), 0);
        if (received < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                break;
            }
            std::perror("recv from UDP failed");
            break;
        }
        if (received == 0) {
            break;
        }

        if (received % static_cast<ssize_t>(kUdpFrameSize) != 0) {
            std::fprintf(stderr, "[UDP:%zu] payload length %zd not multiple of %zu\n",
                         port_index,
                         received,
                         kUdpFrameSize);
        }

        std::size_t offset = 0;
        while (offset + kUdpFrameSize <= static_cast<std::size_t>(received)) {
            struct can_frame frame{};
            if (!decode_udp_frame(port.rx_buffer.data() + offset, frame)) {
                std::fprintf(stderr, "[UDP:%zu] failed to decode frame at offset %zu\n", port_index, offset);
                offset += kUdpFrameSize;
                continue;
            }

            const std::uint32_t can_id = extract_identifier(frame);
            const std::size_t channel_index = find_channel_for_can_id(can_id);
            if (channel_index == kInvalidChannelIndex) {
                std::fprintf(stderr, "[UDP:%zu] no channel mapping for CAN id 0x%08X\n",
                             port_index,
                             static_cast<unsigned int>(can_id));
                offset += kUdpFrameSize;
                continue;
            }

            ChannelContext &channel = channels_[channel_index];
            if (channel.port_index != port_index) {
                std::fprintf(stderr,
                             "[UDP:%zu] channel %zu belongs to port %zu for CAN id 0x%08X\n",
                             port_index,
                             channel_index,
                             channel.port_index,
                             static_cast<unsigned int>(can_id));
                offset += kUdpFrameSize;
                continue;
            }

            const ssize_t written = write(channel.can_fd, &frame, sizeof(frame));
            if (written < 0) {
                if (errno != EAGAIN && errno != EWOULDBLOCK) {
                    std::perror("write to CAN failed");
                }
                break;
            }
            offset += kUdpFrameSize;
        }
    }
}

void BridgeApp::handle_can_events(std::size_t channel_index) {
    if (channel_index >= channels_.size()) {
        return;
    }

    ChannelContext &channel = channels_[channel_index];
    UdpPortContext &port = udp_ports_[channel.port_index];

    while (true) {
        struct can_frame frame{};
        const ssize_t bytes = read(channel.can_fd, &frame, sizeof(frame));
        if (bytes < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                break;
            }
            std::perror("read from CAN failed");
            break;
        }
        if (bytes == 0) {
            break;
        }
        if (bytes != sizeof(frame)) {
            std::fprintf(stderr, "[CAN:%zu] unexpected frame length %zd\n", channel_index, bytes);
            continue;
        }

        if (!encode_udp_frame(frame, tx_buffer_.data())) {
            std::fprintf(stderr, "[CAN:%zu] failed to encode CAN frame\n", channel_index);
            continue;
        }

        const ssize_t sent = sendto(port.udp_fd,
                                    tx_buffer_.data(),
                                    kUdpFrameSize,
                                    0,
                                    reinterpret_cast<const sockaddr *>(&port.remote_addr),
                                    sizeof(port.remote_addr));
        if (sent < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                std::perror("send UDP failed");
            }
            break;
        }
    }
}

std::size_t BridgeApp::find_channel_for_can_id(std::uint32_t can_id) const {
    if (id_lookup_.empty()) {
        return kInvalidChannelIndex;
    }

    std::size_t low = 0;
    std::size_t high = id_lookup_.size();
    while (low < high) {
        const std::size_t mid = (low + high) / 2;
        if (id_lookup_[mid].range.min <= can_id) {
            low = mid + 1;
        } else {
            high = mid;
        }
    }

    if (low == 0) {
        return kInvalidChannelIndex;
    }

    const RangeLookup &candidate = id_lookup_[low - 1];
    if (candidate.range.min <= can_id && can_id <= candidate.range.max) {
        return candidate.channel_index;
    }
    return kInvalidChannelIndex;
}

std::uint32_t BridgeApp::extract_identifier(const struct can_frame &frame) {
    if ((frame.can_id & CAN_EFF_FLAG) != 0U) {
        return frame.can_id & CAN_EFF_MASK;
    }
    return frame.can_id & CAN_SFF_MASK;
}

std::uint64_t BridgeApp::make_event_tag(EventType type, std::uint32_t index) {
    constexpr std::uint64_t kTypeShift = 32U;
    return (static_cast<std::uint64_t>(static_cast<std::uint16_t>(type)) << kTypeShift) |
           static_cast<std::uint64_t>(index);
}

BridgeApp::EventType BridgeApp::decode_event_type(std::uint64_t tag) {
    constexpr std::uint64_t kTypeShift = 32U;
    return static_cast<EventType>(tag >> kTypeShift);
}

std::uint32_t BridgeApp::decode_event_index(std::uint64_t tag) {
    return static_cast<std::uint32_t>(tag & 0xFFFFFFFFULL);
}
