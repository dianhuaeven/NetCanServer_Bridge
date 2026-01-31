#pragma once

#include "config.hpp"
#include "protocol.hpp"

#include <array>
#include <atomic>
#include <cstdint>

#include <netinet/in.h>

class BridgeApp {
public:
    BridgeApp(const ServerConfig &server, const PortConfig &port, const ChannelConfig &channel);
    ~BridgeApp();

    bool initialize();
    void run(std::atomic<bool> &keep_running);

private:
    bool configure_udp_socket();
    bool configure_can_socket();
    bool register_epoll_events();
    void shutdown();

    void handle_udp_events();
    void handle_can_events();

    ServerConfig server_;
    PortConfig port_;
    ChannelConfig channel_;
    int udp_fd_;
    int can_fd_;
    int epoll_fd_;
    sockaddr_in remote_addr_;
    std::array<std::uint8_t, 2048> udp_buffer_;
    std::array<std::uint8_t, kUdpFrameSize> tx_buffer_;
};
