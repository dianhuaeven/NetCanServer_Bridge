#pragma once

#include <cstdint>
#include <string>
#include <vector>

struct IdRange {
    std::uint32_t min{0};
    std::uint32_t max{0};
};

struct ChannelConfig {
    std::string vcan_name;
    std::uint32_t tx_channel_id{0};
    IdRange id_range{};
    std::uint32_t bitrate{0};
};

struct PortConfig {
    std::uint16_t listen_port{0};
    std::uint16_t send_port{0};
    std::vector<ChannelConfig> channels;
};

struct ServerConfig {
    std::string ip;
    std::uint32_t heartbeat_ms{0};
    std::uint32_t reconnect_timeout_ms{0};
};

struct BridgeConfig {
    ServerConfig server{};
    std::vector<PortConfig> ports;
};

bool load_bridge_config(const std::string &path, BridgeConfig &config, std::string &error_message);
