#pragma once

#include "config.hpp"
#include "protocol.hpp"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>

#include <netinet/in.h>

class BridgeApp {
public:
    explicit BridgeApp(const BridgeConfig &config);
    ~BridgeApp();

    bool initialize();
    void run(std::atomic<bool> &keep_running);

private:
    enum class EventType : std::uint16_t {
        Udp = 1,
        Can = 2,
    };

    static constexpr std::size_t kMaxUdpPorts = 8;
    static constexpr std::size_t kMaxChannels = 32;
    static constexpr std::size_t kMaxEvents = kMaxUdpPorts + kMaxChannels;
    static constexpr std::size_t kInvalidChannelIndex = static_cast<std::size_t>(-1);

    struct UdpPortContext {
        PortConfig config;
        int udp_fd{-1};
        sockaddr_in remote_addr{};
        std::array<std::uint8_t, 4096> rx_buffer{};
    };

    struct ChannelContext {
        ChannelConfig config;
        int can_fd{-1};
        std::size_t port_index{0};
    };

    struct RangeLookup {
        IdRange range{};
        std::size_t channel_index{0};
    };

    bool configure_udp_socket(UdpPortContext &context);
    bool configure_can_socket(ChannelContext &context);
    bool prepare_can_interface(const ChannelConfig &config) const;
    bool register_event(EventType type, std::uint32_t index, int fd);
    void shutdown();

    void handle_udp_events(std::size_t port_index);
    void handle_can_events(std::size_t channel_index);

    std::size_t find_channel_for_can_id(std::uint32_t can_id) const;
    static std::uint32_t extract_identifier(const struct can_frame &frame);
    static std::uint64_t make_event_tag(EventType type, std::uint32_t index);
    static EventType decode_event_type(std::uint64_t tag);
    static std::uint32_t decode_event_index(std::uint64_t tag);

    BridgeConfig config_;
    int epoll_fd_;
    std::array<UdpPortContext, kMaxUdpPorts> udp_ports_;
    std::array<ChannelContext, kMaxChannels> channels_;
    std::array<RangeLookup, kMaxChannels> id_lookup_;
    std::size_t udp_port_count_;
    std::size_t channel_count_;
    std::size_t id_lookup_count_;
    std::array<std::uint8_t, kUdpFrameSize> tx_buffer_;
};
