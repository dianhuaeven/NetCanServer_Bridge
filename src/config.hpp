#pragma once

#include <array>
#include <cstdint>
#include <string>

struct BridgeConfig {
    std::array<char, 64> server_ip{};
    uint16_t listen_port{0};
    uint16_t send_port{0};
    std::array<char, 32> vcan_name{};
};

bool load_bridge_config(const std::string &path, BridgeConfig &config, std::string &error_message);
