#include "config.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>

namespace {

bool read_file(const std::string &path, std::string &output) {
    std::ifstream input(path, std::ios::binary);
    if (!input.is_open()) {
        return false;
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    output = buffer.str();
    return true;
}

const char *advance_to_quote(const std::string &text, std::size_t start) {
    auto pos = text.find('"', start);
    if (pos == std::string::npos) {
        return nullptr;
    }
    return text.data() + pos;
}

template <std::size_t N>
bool extract_string(const std::string &text, const std::string &key, std::array<char, N> &dest) {
    auto pos = text.find(key);
    if (pos == std::string::npos) {
        return false;
    }
    const char *first_quote = advance_to_quote(text, pos + key.size());
    if (first_quote == nullptr) {
        return false;
    }
    const char *second_quote = advance_to_quote(text, (first_quote - text.data()) + 1);
    if (second_quote == nullptr) {
        return false;
    }
    const std::size_t length = static_cast<std::size_t>(second_quote - first_quote - 1);
    if (length + 1 > dest.size()) {
        return false;
    }
    std::fill(dest.begin(), dest.end(), '\0');
    if (length > 0) {
        std::memcpy(dest.data(), first_quote + 1, length);
    }
    return true;
}

bool extract_uint16(const std::string &text, const std::string &key, uint16_t &value) {
    auto pos = text.find(key);
    if (pos == std::string::npos) {
        return false;
    }
    pos += key.size();
    while (pos < text.size() && !std::isdigit(static_cast<unsigned char>(text[pos])) && text[pos] != 'x' && text[pos] != 'X') {
        ++pos;
    }
    if (pos >= text.size()) {
        return false;
    }
    const char *start = text.data() + pos;
    char *end = nullptr;
    unsigned long parsed = std::strtoul(start, &end, 0);
    if (end == start) {
        return false;
    }
    if (parsed > 0xFFFFu) {
        return false;
    }
    value = static_cast<uint16_t>(parsed);
    return true;
}

} // namespace

bool load_bridge_config(const std::string &path, BridgeConfig &config, std::string &error_message) {
    std::string file_content;
    if (!read_file(path, file_content)) {
        error_message = "unable to read config file: " + path;
        return false;
    }

    if (!extract_string(file_content, "\"ip\"", config.server_ip)) {
        error_message = "failed to parse server ip";
        return false;
    }

    uint16_t listen_port = 0;
    uint16_t send_port = 0;
    bool has_listen = extract_uint16(file_content, "\"udp_listen_port\"", listen_port);
    bool has_send = extract_uint16(file_content, "\"udp_send_port\"", send_port);
    uint16_t legacy_port = 0;
    const bool has_legacy = extract_uint16(file_content, "\"udp_port\"", legacy_port);

    if (!has_listen && has_legacy) {
        listen_port = legacy_port;
        has_listen = true;
    }

    if (!has_send) {
        if (has_legacy) {
            send_port = legacy_port;
            has_send = true;
        } else if (has_listen) {
            send_port = listen_port;
            has_send = true;
        }
    }

    if (!has_listen) {
        error_message = "failed to parse udp_listen_port";
        return false;
    }
    if (!has_send) {
        error_message = "failed to parse udp_send_port";
        return false;
    }

    config.listen_port = listen_port;
    config.send_port = send_port;

    if (!extract_string(file_content, "\"vcan_name\"", config.vcan_name)) {
        error_message = "failed to parse vcan_name";
        return false;
    }

    return true;
}
