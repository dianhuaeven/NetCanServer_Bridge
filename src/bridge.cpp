#include "bridge.hpp"

#include <algorithm>
#include <array>
#include <arpa/inet.h>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <string>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <syslog.h>
#include <unistd.h>

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

void log_errno(const char *message) {
    syslog(LOG_ERR, "%s: %s", message, std::strerror(errno));
}

bool interface_exists(const std::string &name) {
    return if_nametoindex(name.c_str()) != 0U;
}

} // namespace

BridgeApp::BridgeApp(const BridgeConfig &config)
    : config_(config),
      epoll_fd_(-1),
      udp_port_count_(0),
      channel_count_(0),
      id_lookup_count_(0) {
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
        log_errno("failed to create epoll instance");
        return false;
    }

    udp_port_count_ = 0;
    channel_count_ = 0;
    id_lookup_count_ = 0;

    for (const auto &port_cfg : config_.ports) {
        if (udp_port_count_ >= kMaxUdpPorts) {
            syslog(LOG_ERR, "configured UDP ports exceed supported maximum (%zu)", kMaxUdpPorts);
            shutdown();
            return false;
        }

        UdpPortContext &port_ctx = udp_ports_[udp_port_count_];
        port_ctx = {};
        port_ctx.config = port_cfg;
        port_ctx.remote_addr.sin_family = AF_INET;
        port_ctx.remote_addr.sin_addr = server_addr;
        port_ctx.remote_addr.sin_port = htons(port_cfg.send_port);
        port_ctx.rx_buffer.fill(0);

        if (!configure_udp_socket(port_ctx)) {
            shutdown();
            return false;
        }

        const std::size_t port_index = udp_port_count_;
        ++udp_port_count_;

        if (!register_event(EventType::Udp, static_cast<std::uint32_t>(port_index), port_ctx.udp_fd)) {
            shutdown();
            return false;
        }

        syslog(LOG_INFO,
               "[UDP:%zu] listen 0.0.0.0:%u -> %s:%u",
               port_index,
               port_ctx.config.listen_port,
               config_.server.ip.c_str(),
               port_ctx.config.send_port);

        for (const auto &channel_cfg : port_cfg.channels) {
            if (channel_count_ >= kMaxChannels) {
                syslog(LOG_ERR, "configured channels exceed supported maximum (%zu)", kMaxChannels);
                shutdown();
                return false;
            }

            ChannelContext &channel_ctx = channels_[channel_count_];
            channel_ctx = {};
            channel_ctx.config = channel_cfg;
            channel_ctx.port_index = port_index;

            if (!prepare_can_interface(channel_ctx.config)) {
                shutdown();
                return false;
            }

            if (!configure_can_socket(channel_ctx)) {
                shutdown();
                return false;
            }

            const std::size_t channel_index = channel_count_;
            ++channel_count_;

            if (!register_event(EventType::Can, static_cast<std::uint32_t>(channel_index), channels_[channel_index].can_fd)) {
                shutdown();
                return false;
            }

            if (id_lookup_count_ >= kMaxChannels) {
                syslog(LOG_ERR, "identifier lookup table overflow");
                shutdown();
                return false;
            }

            RangeLookup &lookup = id_lookup_[id_lookup_count_];
            lookup.range = channel_cfg.id_range;
            lookup.channel_index = channel_index;
            ++id_lookup_count_;

            syslog(LOG_INFO,
                   "[CAN:%zu] %s range[0x%08X,0x%08X] -> UDP port %zu",
                   channel_index,
                   channel_ctx.config.vcan_name.c_str(),
                   channel_ctx.config.id_range.min,
                   channel_ctx.config.id_range.max,
                   channel_ctx.port_index);
        }
    }

    if (id_lookup_count_ > 1) {
        std::sort(id_lookup_.begin(),
                  id_lookup_.begin() + static_cast<std::ptrdiff_t>(id_lookup_count_),
                  [](const RangeLookup &lhs, const RangeLookup &rhs) { return lhs.range.min < rhs.range.min; });
    }

    return true;
}

void BridgeApp::run(std::atomic<bool> &keep_running) {
    if (epoll_fd_ < 0) {
        return;
    }

    const std::size_t max_events = udp_port_count_ + channel_count_;
    if (max_events == 0) {
        return;
    }

    std::array<epoll_event, kMaxEvents> events{};
    while (keep_running.load()) {
        const int ready = epoll_wait(epoll_fd_, events.data(), static_cast<int>(max_events), 1000);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            log_errno("epoll_wait failed");
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
                if (index < udp_port_count_) {
                    handle_udp_events(index);
                }
                break;
            case EventType::Can:
                if (index < channel_count_) {
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
        log_errno("failed to create UDP socket");
        return false;
    }
    if (!set_non_blocking(context.udp_fd)) {
        log_errno("failed to set UDP non-blocking");
        close_fd(context.udp_fd);
        return false;
    }

    int opt = 1;
    if (setsockopt(context.udp_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        log_errno("setsockopt SO_REUSEADDR failed");
    }

    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = INADDR_ANY;
    local.sin_port = htons(context.config.listen_port);
    if (bind(context.udp_fd, reinterpret_cast<sockaddr *>(&local), sizeof(local)) < 0) {
        log_errno("failed to bind UDP socket");
        close_fd(context.udp_fd);
        return false;
    }

    return true;
}

bool BridgeApp::configure_can_socket(ChannelContext &context) {
    context.can_fd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
    if (context.can_fd < 0) {
        log_errno("failed to create CAN socket");
        return false;
    }
    if (!set_non_blocking(context.can_fd)) {
        log_errno("failed to set CAN non-blocking");
        close_fd(context.can_fd);
        return false;
    }

    ifreq ifr{};
    std::strncpy(ifr.ifr_name, context.config.vcan_name.c_str(), sizeof(ifr.ifr_name));
    ifr.ifr_name[sizeof(ifr.ifr_name) - 1] = '\0';
    if (ioctl(context.can_fd, SIOCGIFINDEX, &ifr) < 0) {
        log_errno("failed to lookup CAN interface");
        close_fd(context.can_fd);
        return false;
    }

    sockaddr_can addr{};
    addr.can_family = AF_CAN;
    addr.can_ifindex = ifr.ifr_ifindex;
    if (bind(context.can_fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        log_errno("failed to bind CAN socket");
        close_fd(context.can_fd);
        return false;
    }

    return true;
}

bool BridgeApp::prepare_can_interface(const ChannelConfig &config) const {
    if (!interface_exists(config.vcan_name)) {
        syslog(LOG_ERR, "required CAN interface %s not found", config.vcan_name.c_str());
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
        log_errno("failed to register fd with epoll");
        return false;
    }
    return true;
}

void BridgeApp::shutdown() {
    for (std::size_t i = 0; i < channel_count_; ++i) {
        close_fd(channels_[i].can_fd);
    }
    for (std::size_t i = 0; i < udp_port_count_; ++i) {
        close_fd(udp_ports_[i].udp_fd);
    }
    close_fd(epoll_fd_);
    udp_port_count_ = 0;
    channel_count_ = 0;
    id_lookup_count_ = 0;
}

void BridgeApp::handle_udp_events(std::size_t port_index) {
    if (port_index >= udp_port_count_) {
        return;
    }

    UdpPortContext &port = udp_ports_[port_index];
    while (true) {
        const ssize_t received = recv(port.udp_fd, port.rx_buffer.data(), port.rx_buffer.size(), 0);
        if (received < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                break;
            }
            log_errno("recv from UDP failed");
            break;
        }
        if (received == 0) {
            break;
        }

        if (received % static_cast<ssize_t>(kUdpFrameSize) != 0) {
            syslog(LOG_WARNING,
                   "[UDP:%zu] payload length %zd not multiple of %zu",
                   port_index,
                   received,
                   kUdpFrameSize);
        }

        std::size_t offset = 0;
        while (offset + kUdpFrameSize <= static_cast<std::size_t>(received)) {
            struct can_frame frame{};
            if (!decode_udp_frame(port.rx_buffer.data() + offset, frame)) {
                syslog(LOG_WARNING, "[UDP:%zu] failed to decode frame at offset %zu", port_index, offset);
                offset += kUdpFrameSize;
                continue;
            }

            const std::uint32_t can_id = extract_identifier(frame);
            const std::size_t channel_index = find_channel_for_can_id(can_id);
            if (channel_index == kInvalidChannelIndex) {
                syslog(LOG_WARNING,
                       "[UDP:%zu] no channel mapping for CAN id 0x%08X",
                       port_index,
                       static_cast<unsigned int>(can_id));
                offset += kUdpFrameSize;
                continue;
            }

            ChannelContext &channel = channels_[channel_index];
            if (channel.port_index != port_index) {
                syslog(LOG_WARNING,
                       "[UDP:%zu] channel %zu belongs to port %zu for CAN id 0x%08X",
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
                    log_errno("write to CAN failed");
                }
                break;
            }
            offset += kUdpFrameSize;
        }
    }
}

void BridgeApp::handle_can_events(std::size_t channel_index) {
    if (channel_index >= channel_count_) {
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
            log_errno("read from CAN failed");
            break;
        }
        if (bytes == 0) {
            break;
        }
        if (bytes != sizeof(frame)) {
            syslog(LOG_WARNING, "[CAN:%zu] unexpected frame length %zd", channel_index, bytes);
            continue;
        }

        if (!encode_udp_frame(frame, tx_buffer_.data())) {
            syslog(LOG_WARNING, "[CAN:%zu] failed to encode CAN frame", channel_index);
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
                log_errno("send UDP failed");
            }
            break;
        }
    }
}

std::size_t BridgeApp::find_channel_for_can_id(std::uint32_t can_id) const {
    if (id_lookup_count_ == 0) {
        return kInvalidChannelIndex;
    }

    std::size_t low = 0;
    std::size_t high = id_lookup_count_;
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
