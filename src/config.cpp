#include "config.hpp"

#include <cerrno>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <limits>
#include <memory>
#include <set>
#include <sstream>
#include <string>
#include <utility>

#include <jsoncpp/json/json.h>

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

bool parse_hex_uint32(const std::string &text, std::uint32_t &value) {
    const char *begin = text.c_str();
    char *end = nullptr;
    errno = 0;
    unsigned long parsed = std::strtoul(begin, &end, 0);
    if (errno != 0 || end == begin || *end != '\0') {
        return false;
    }
    if (parsed > std::numeric_limits<std::uint32_t>::max()) {
        return false;
    }
    value = static_cast<std::uint32_t>(parsed);
    return true;
}

bool parse_server(const Json::Value &node, ServerConfig &server, std::string &error_message) {
    if (!node.isObject()) {
        error_message = "server must be an object";
        return false;
    }
    const auto &ip = node["ip"];
    if (!ip.isString() || ip.asString().empty()) {
        error_message = "server.ip must be a non-empty string";
        return false;
    }
    server.ip = ip.asString();

    const auto parse_uint32_field = [&](const char *name, std::uint32_t &dest) -> bool {
        const auto &value = node[name];
        if (!value.isUInt()) {
            error_message = std::string("server.") + name + " must be an unsigned integer";
            return false;
        }
        dest = value.asUInt();
        if (dest == 0) {
            error_message = std::string("server.") + name + " must be > 0";
            return false;
        }
        return true;
    };

    if (!parse_uint32_field("heartbeat_ms", server.heartbeat_ms)) {
        return false;
    }
    if (!parse_uint32_field("reconnect_timeout_ms", server.reconnect_timeout_ms)) {
        return false;
    }
    return true;
}

bool parse_channel(const Json::Value &node,
                   ChannelConfig &channel,
                   std::set<std::string> &global_vcan_names,
                   std::set<std::uint32_t> &channel_ids,
                   std::vector<IdRange> &ranges,
                   const std::string &context,
                   std::string &error_message) {
    if (!node.isObject()) {
        error_message = context + " must be an object";
        return false;
    }

    const auto &vcan = node["vcan_name"];
    if (!vcan.isString() || vcan.asString().empty()) {
        error_message = context + ".vcan_name must be a non-empty string";
        return false;
    }
    channel.vcan_name = vcan.asString();
    if (!global_vcan_names.insert(channel.vcan_name).second) {
        error_message = "duplicated vcan interface: " + channel.vcan_name;
        return false;
    }

    const auto &tx_id_val = node["tx_channel_id"];
    if (!tx_id_val.isUInt()) {
        error_message = context + ".tx_channel_id must be an unsigned integer";
        return false;
    }
    channel.tx_channel_id = tx_id_val.asUInt();
    if (!channel_ids.insert(channel.tx_channel_id).second) {
        error_message = context + ": duplicated tx_channel_id " + std::to_string(channel.tx_channel_id);
        return false;
    }

    const auto &bitrate_val = node["bitrate"];
    if (!bitrate_val.isUInt()) {
        error_message = context + ".bitrate must be an unsigned integer";
        return false;
    }
    channel.bitrate = bitrate_val.asUInt();
    if (channel.bitrate == 0) {
        error_message = context + ".bitrate must be > 0";
        return false;
    }

    const auto &range_node = node["id_range"];
    if (!range_node.isObject()) {
        error_message = context + ".id_range must be an object";
        return false;
    }

    const auto parse_range_value = [&](const char *key, std::uint32_t &dest) -> bool {
        const auto &value = range_node[key];
        if (!value.isString()) {
            error_message = context + ".id_range." + key + " must be a string";
            return false;
        }
        std::uint32_t parsed = 0;
        if (!parse_hex_uint32(value.asString(), parsed)) {
            error_message = context + ".id_range." + key + " must be a valid hex/decimal value";
            return false;
        }
        dest = parsed;
        return true;
    };

    if (!parse_range_value("min", channel.id_range.min)) {
        return false;
    }
    if (!parse_range_value("max", channel.id_range.max)) {
        return false;
    }
    if (channel.id_range.min > channel.id_range.max) {
        error_message = context + ": id_range.min must be <= id_range.max";
        return false;
    }
    if (channel.id_range.max > 0x1FFFFFFFu) {
        error_message = context + ": id_range.max exceeds 29-bit CAN limit";
        return false;
    }

    for (const auto &existing : ranges) {
        if (!(channel.id_range.max < existing.min || channel.id_range.min > existing.max)) {
            error_message = context + ": id_range overlaps with another channel";
            return false;
        }
    }
    ranges.push_back(channel.id_range);
    return true;
}

bool parse_port(const Json::Value &node,
                PortConfig &port,
                std::set<std::uint16_t> &listen_ports,
                std::set<std::string> &global_vcan_names,
                const std::string &context,
                std::string &error_message) {
    if (!node.isObject()) {
        error_message = context + " must be an object";
        return false;
    }

    const auto parse_port_field = [&](const char *field_name, std::uint16_t &dest) -> bool {
        const auto &value = node[field_name];
        if (value.isNull()) {
            return false;
        }
        if (!value.isUInt()) {
            error_message = context + "." + field_name + " must be an unsigned integer";
            return false;
        }
        const auto parsed = value.asUInt();
        if (parsed == 0 || parsed > 65535) {
            error_message = context + "." + field_name + " must be within [1,65535]";
            return false;
        }
        dest = static_cast<std::uint16_t>(parsed);
        return true;
    };

    std::uint16_t listen_port = 0;
    std::uint16_t send_port = 0;
    bool has_listen = parse_port_field("udp_listen_port", listen_port);
    bool has_send = parse_port_field("udp_send_port", send_port);
    std::uint16_t legacy_port = 0;
    const bool has_legacy = parse_port_field("udp_port", legacy_port);

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
        error_message = context + " is missing udp_listen_port (or legacy udp_port)";
        return false;
    }
    if (!has_send) {
        error_message = context + " is missing udp_send_port (or legacy udp_port)";
        return false;
    }

    port.listen_port = listen_port;
    port.send_port = send_port;

    if (!listen_ports.insert(port.listen_port).second) {
        error_message = "duplicated udp_listen_port: " + std::to_string(port.listen_port);
        return false;
    }

    const auto &channels = node["channels"];
    if (!channels.isArray() || channels.empty()) {
        error_message = context + ".channels must be a non-empty array";
        return false;
    }

    std::set<std::uint32_t> channel_ids;
    std::vector<IdRange> ranges;
    port.channels.reserve(channels.size());
    for (Json::ArrayIndex i = 0; i < channels.size(); ++i) {
        ChannelConfig channel{};
        const std::string chan_ctx = context + ".channels[" + std::to_string(i) + "]";
        if (!parse_channel(channels[i], channel, global_vcan_names, channel_ids, ranges, chan_ctx, error_message)) {
            return false;
        }
        port.channels.push_back(std::move(channel));
    }

    return true;
}

bool parse_ports(const Json::Value &node, std::vector<PortConfig> &ports, std::string &error_message) {
    if (!node.isArray() || node.empty()) {
        error_message = "ports must be a non-empty array";
        return false;
    }

    std::set<std::uint16_t> listen_ports;
    std::set<std::string> global_vcan_names;

    ports.reserve(node.size());
    for (Json::ArrayIndex i = 0; i < node.size(); ++i) {
        PortConfig port{};
        const std::string context = "ports[" + std::to_string(i) + "]";
        if (!parse_port(node[i], port, listen_ports, global_vcan_names, context, error_message)) {
            return false;
        }
        ports.push_back(std::move(port));
    }
    return true;
}

} // namespace

bool load_bridge_config(const std::string &path, BridgeConfig &config, std::string &error_message) {
    std::string file_content;
    if (!read_file(path, file_content)) {
        error_message = "unable to read config file: " + path;
        return false;
    }

    Json::CharReaderBuilder builder;
    builder["collectComments"] = false;
    std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
    Json::Value root;
    std::string parse_errors;
    if (!reader->parse(file_content.data(), file_content.data() + file_content.size(), &root, &parse_errors)) {
        error_message = "invalid JSON: " + parse_errors;
        return false;
    }
    if (!root.isObject()) {
        error_message = "config root must be an object";
        return false;
    }

    BridgeConfig parsed{};
    if (!parse_server(root["server"], parsed.server, error_message)) {
        return false;
    }
    if (!parse_ports(root["ports"], parsed.ports, error_message)) {
        return false;
    }

    config = std::move(parsed);
    return true;
}
