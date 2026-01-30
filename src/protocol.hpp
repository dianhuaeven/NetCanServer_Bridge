#pragma once

#include <cstddef>
#include <cstdint>

#include <linux/can.h>

constexpr std::size_t kUdpFrameSize = 13;

bool decode_udp_frame(const std::uint8_t *data, struct can_frame &frame);
bool encode_udp_frame(const struct can_frame &frame, std::uint8_t *buffer);
