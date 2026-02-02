#include "config.hpp"
#include "protocol.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <linux/can.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unistd.h>
#include <vector>

namespace {

int g_failures = 0;

void report_failure(const char *test_name, const char *message) {
    std::fprintf(stderr, "[FAIL] %s: %s\n", test_name, message);
    ++g_failures;
}

void expect_true(bool condition, const char *test_name, const char *message) {
    if (!condition) {
        report_failure(test_name, message);
    }
}

std::string write_temp_file(std::string_view content) {
    char path[] = "/tmp/bridge_unitXXXXXX";
    const int fd = mkstemp(path);
    if (fd < 0) {
        throw std::runtime_error("mkstemps failed");
    }
    const ssize_t written = write(fd, content.data(), static_cast<size_t>(content.size()));
    if (written < 0 || static_cast<size_t>(written) != content.size()) {
        close(fd);
        unlink(path);
        throw std::runtime_error("failed to write temp file");
    }
    close(fd);
    return std::string(path);
}

void remove_file(const std::string &path) {
    unlink(path.c_str());
}

bool test_valid_config_parses() {
    constexpr const char *kTestName = "valid_config_parses";
    const char json[] = R"JSON(
{
  "server": { "ip": "10.0.0.5" },
  "ports": [
    {
      "udp_listen_port": 5555,
      "udp_send_port": 5556,
      "channels": [
        {
          "vcan_name": "vcan0",
          "tx_channel_id": 1,
          "id_range": { "min": "0x100", "max": "0x1FF" },
          "bitrate": 500000
        }
      ]
    }
  ]
}
)JSON";
    const std::string file_path = write_temp_file(json);

    BridgeConfig cfg{};
    std::string error;
    const bool ok = load_bridge_config(file_path, cfg, error);
    remove_file(file_path);

    expect_true(ok, kTestName, error.c_str());
    expect_true(cfg.server.ip == "10.0.0.5", kTestName, "server ip mismatch");
    expect_true(cfg.ports.size() == 1, kTestName, "unexpected port count");
    expect_true(cfg.ports[0].channels.size() == 1, kTestName, "unexpected channel count");
    expect_true(cfg.ports[0].channels[0].id_range.min == 0x100, kTestName, "range min mismatch");
    expect_true(cfg.ports[0].channels[0].id_range.max == 0x1FF, kTestName, "range max mismatch");
    return true;
}

bool test_missing_ports_is_error() {
    constexpr const char *kTestName = "missing_ports_is_error";
    const char json[] = R"JSON(
{
  "server": { "ip": "10.0.0.5" }
}
)JSON";
    const std::string file_path = write_temp_file(json);

    BridgeConfig cfg{};
    std::string error;
    const bool ok = load_bridge_config(file_path, cfg, error);
    remove_file(file_path);

    expect_true(!ok, kTestName, "parser unexpectedly succeeded");
    expect_true(error.find("ports") != std::string::npos, kTestName, "error message missing keyword");
    return true;
}

bool test_overlapping_id_ranges_fail() {
    constexpr const char *kTestName = "overlapping_id_ranges_fail";
    const char json[] = R"JSON(
{
  "server": { "ip": "10.0.0.5" },
  "ports": [
    {
      "udp_port": 6000,
      "channels": [
        {
          "vcan_name": "vcan0",
          "tx_channel_id": 0,
          "id_range": { "min": "0x100", "max": "0x1FF" },
          "bitrate": 500000
        },
        {
          "vcan_name": "vcan1",
          "tx_channel_id": 1,
          "id_range": { "min": "0x1F0", "max": "0x2FF" },
          "bitrate": 500000
        }
      ]
    }
  ]
}
)JSON";
    const std::string file_path = write_temp_file(json);

    BridgeConfig cfg{};
    std::string error;
    const bool ok = load_bridge_config(file_path, cfg, error);
    remove_file(file_path);

    expect_true(!ok, kTestName, "expected parser failure for overlapping ranges");
    expect_true(error.find("id_range") != std::string::npos, kTestName, "error message missing id_range hint");
    return true;
}

bool test_protocol_roundtrip_standard() {
    constexpr const char *kTestName = "protocol_roundtrip_standard";
    struct can_frame frame{};
    frame.can_id = 0x123;
    frame.can_dlc = 8;
    for (int i = 0; i < 8; ++i) {
        frame.data[i] = static_cast<std::uint8_t>(i);
    }

    std::uint8_t buffer[kUdpFrameSize]{};
    expect_true(encode_udp_frame(frame, buffer), kTestName, "encode failed");

    struct can_frame decoded{};
    expect_true(decode_udp_frame(buffer, decoded), kTestName, "decode failed");
    expect_true((decoded.can_id & CAN_SFF_MASK) == 0x123, kTestName, "decoded id mismatch");
    expect_true(decoded.can_dlc == 8, kTestName, "decoded dlc mismatch");
    expect_true(std::memcmp(decoded.data, frame.data, 8) == 0, kTestName, "decoded data mismatch");
    return true;
}

bool test_protocol_roundtrip_extended() {
    constexpr const char *kTestName = "protocol_roundtrip_extended";
    struct can_frame frame{};
    frame.can_id = 0x1ABCDE00 | CAN_EFF_FLAG | CAN_RTR_FLAG;
    frame.can_dlc = 4;
    frame.data[0] = 0xDE;
    frame.data[1] = 0xAD;
    frame.data[2] = 0xBE;
    frame.data[3] = 0xEF;

    std::uint8_t buffer[kUdpFrameSize]{};
    expect_true(encode_udp_frame(frame, buffer), kTestName, "encode failed");

    struct can_frame decoded{};
    expect_true(decode_udp_frame(buffer, decoded), kTestName, "decode failed");
    expect_true((decoded.can_id & CAN_EFF_FLAG) != 0, kTestName, "EFF flag missing");
    expect_true((decoded.can_id & CAN_EFF_MASK) == 0x1ABCDE00, kTestName, "id mismatch");
    expect_true((decoded.can_id & CAN_RTR_FLAG) != 0, kTestName, "RTR flag missing");
    expect_true(decoded.can_dlc == 4, kTestName, "dlc mismatch");
    expect_true(std::memcmp(decoded.data, frame.data, 4) == 0, kTestName, "data mismatch");
    return true;
}

bool test_decode_rejects_large_dlc() {
    constexpr const char *kTestName = "decode_rejects_large_dlc";
    std::uint8_t buffer[kUdpFrameSize]{};
    buffer[0] = 0x09; // dlc = 9
    struct can_frame frame{};
    bool ok = decode_udp_frame(buffer, frame);
    expect_true(!ok, kTestName, "decode should reject DLC > 8");
    return true;
}

} // namespace

int main() {
    test_valid_config_parses();
    test_missing_ports_is_error();
    test_overlapping_id_ranges_fail();
    test_protocol_roundtrip_standard();
    test_protocol_roundtrip_extended();
    test_decode_rejects_large_dlc();

    if (g_failures == 0) {
        std::puts("All tests passed.");
        return 0;
    }
    std::fprintf(stderr, "%d test(s) failed.\n", g_failures);
    return 1;
}
