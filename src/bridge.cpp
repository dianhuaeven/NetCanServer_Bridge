#include "bridge.hpp"

#include <arpa/inet.h>
#include <cstdio>
#include <cstring>
#include <errno.h>
#include <fcntl.h>
#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
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

} // namespace

BridgeApp::BridgeApp(const BridgeConfig &config)
    : config_(config),
      udp_fd_(-1),
      can_fd_(-1),
      epoll_fd_(-1),
      remote_addr_{} {
    udp_buffer_.fill(0);
    tx_buffer_.fill(0);
}

BridgeApp::~BridgeApp() {
    shutdown();
}

bool BridgeApp::initialize() {
    udp_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd_ < 0) {
        std::perror("failed to create UDP socket");
        return false;
    }

    can_fd_ = socket(PF_CAN, SOCK_RAW, CAN_RAW);
    if (can_fd_ < 0) {
        std::perror("failed to create CAN socket");
        shutdown();
        return false;
    }

    if (!configure_udp_socket() || !configure_can_socket()) {
        shutdown();
        return false;
    }

    epoll_fd_ = epoll_create1(0);
    if (epoll_fd_ < 0) {
        std::perror("failed to create epoll instance");
        shutdown();
        return false;
    }

    if (!register_epoll_events()) {
        shutdown();
        return false;
    }

    return true;
}

void BridgeApp::run(std::atomic<bool> &keep_running) {
    while (keep_running.load()) {
        epoll_event events[2];
        const int ready = epoll_wait(epoll_fd_, events, 2, 1000);
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
            if (events[i].data.fd == udp_fd_) {
                handle_udp_events();
            } else if (events[i].data.fd == can_fd_) {
                handle_can_events();
            }
        }
    }
}

bool BridgeApp::configure_udp_socket() {
    if (!set_non_blocking(udp_fd_)) {
        std::perror("failed to set UDP non-blocking");
        return false;
    }

    int opt = 1;
    if (setsockopt(udp_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        std::perror("setsockopt SO_REUSEADDR failed");
    }

    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = INADDR_ANY;
    local.sin_port = htons(config_.listen_port);
    if (bind(udp_fd_, reinterpret_cast<sockaddr *>(&local), sizeof(local)) < 0) {
        std::perror("failed to bind UDP socket (RX will not work without this)");
        return false;
    }

    sockaddr_in remote{};
    remote.sin_family = AF_INET;
    remote.sin_port = htons(config_.send_port);
    if (inet_pton(AF_INET, config_.server_ip.data(), &remote.sin_addr) != 1) {
        std::fprintf(stderr, "invalid server ip address: %s\n", config_.server_ip.data());
        return false;
    }
    remote_addr_ = remote;

    std::printf("[UDP] Listening on 0.0.0.0:%d; default remote %s:%d\n",
                config_.listen_port, config_.server_ip.data(), config_.send_port);
    return true;
}

bool BridgeApp::configure_can_socket() {
    if (!set_non_blocking(can_fd_)) {
        std::perror("failed to set CAN non-blocking");
        return false;
    }

    ifreq ifr{};
    std::strncpy(ifr.ifr_name, config_.vcan_name.data(), sizeof(ifr.ifr_name));
    ifr.ifr_name[sizeof(ifr.ifr_name) - 1] = '\0';
    if (ioctl(can_fd_, SIOCGIFINDEX, &ifr) < 0) {
        std::perror("failed to lookup CAN interface");
        return false;
    }

    sockaddr_can addr{};
    addr.can_family = AF_CAN;
    addr.can_ifindex = ifr.ifr_ifindex;
    if (bind(can_fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        std::perror("failed to bind CAN socket");
        return false;
    }

    return true;
}

bool BridgeApp::register_epoll_events() {
    epoll_event udp_event{};
    udp_event.events = EPOLLIN;
    udp_event.data.fd = udp_fd_;
    if (epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, udp_fd_, &udp_event) < 0) {
        std::perror("failed to register UDP fd");
        return false;
    }

    epoll_event can_event{};
    can_event.events = EPOLLIN;
    can_event.data.fd = can_fd_;
    if (epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, can_fd_, &can_event) < 0) {
        std::perror("failed to register CAN fd");
        return false;
    }

    return true;
}

void BridgeApp::shutdown() {
    close_fd(epoll_fd_);
    close_fd(udp_fd_);
    close_fd(can_fd_);
}

void BridgeApp::handle_udp_events() {
    while (true) {
        const ssize_t received = recv(udp_fd_, udp_buffer_.data(), udp_buffer_.size(), 0);
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
            std::fprintf(stderr, "warning: UDP payload length %zd not multiple of %zu\n",
                         received, kUdpFrameSize);
        }

        std::size_t offset = 0;
        while (offset + kUdpFrameSize <= static_cast<std::size_t>(received)) {
            struct can_frame frame{};
            if (!decode_udp_frame(udp_buffer_.data() + offset, frame)) {
                std::fprintf(stderr, "failed to decode UDP frame at offset %zu\n", offset);
                offset += kUdpFrameSize;
                continue;
            }

            const ssize_t written = write(can_fd_, &frame, sizeof(frame));
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

void BridgeApp::handle_can_events() {
    while (true) {
        struct can_frame frame{};
        const ssize_t bytes = read(can_fd_, &frame, sizeof(frame));
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
            std::fprintf(stderr, "unexpected CAN frame length: %zd\n", bytes);
            continue;
        }

        if (!encode_udp_frame(frame, tx_buffer_.data())) {
            std::fprintf(stderr, "failed to encode CAN frame\n");
            continue;
        }

        const ssize_t sent = sendto(udp_fd_, tx_buffer_.data(), kUdpFrameSize, 0,
                                    reinterpret_cast<const sockaddr *>(&remote_addr_),
                                    sizeof(remote_addr_));
        if (sent < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                std::perror("send UDP failed");
            }
            break;
        }
    }
}
