#include "protocol.hpp"
#include <algorithm>
#include <cstring>

bool decode_udp_frame(const std::uint8_t *data, struct can_frame &frame) {
    if (data == nullptr) {
        return false;
    }

    const std::uint8_t info = data[0];
    const bool is_extended = (info & 0x80U) != 0U;
    const bool is_remote = (info & 0x40U) != 0U;
    const std::uint8_t dlc = info & 0x0FU;
    
    if (dlc > 8U) {
        return false;
    }

    // --- 修改点：改为大端序解析 ID (Big Endian) ---
    // data[1] 是最高位 (MSB)，data[4] 是最低位 (LSB)
    const std::uint32_t raw_id = (static_cast<std::uint32_t>(data[1]) << 24U) |
                                 (static_cast<std::uint32_t>(data[2]) << 16U) |
                                 (static_cast<std::uint32_t>(data[3]) << 8U) |
                                 static_cast<std::uint32_t>(data[4]);

    frame.can_id = 0;
    if (is_extended) {
        frame.can_id = (raw_id & CAN_EFF_MASK) | CAN_EFF_FLAG;
    } else {
        frame.can_id = raw_id & CAN_SFF_MASK;
    }

    if (is_remote) {
        frame.can_id |= CAN_RTR_FLAG;
    }

    frame.can_dlc = dlc;
    std::memset(frame.data, 0, sizeof(frame.data));
    std::memcpy(frame.data, &data[5], dlc);
    return true;
}

bool encode_udp_frame(const struct can_frame &frame, std::uint8_t *buffer) {
    if (buffer == nullptr) {
        return false;
    }

    const std::uint8_t dlc = static_cast<std::uint8_t>(std::min<std::uint8_t>(frame.can_dlc, 8U));
    std::uint8_t info = dlc & 0x0FU;
    std::uint32_t id = 0;

    if ((frame.can_id & CAN_EFF_FLAG) != 0U) {
        info |= 0x80U; // FF = 1 (扩展帧)
        id = frame.can_id & CAN_EFF_MASK;
    } else {
        id = frame.can_id & CAN_SFF_MASK; // FF = 0 (标准帧)
    }

    if ((frame.can_id & CAN_RTR_FLAG) != 0U) {
        info |= 0x40U; // RTR = 1 (远程帧)
    }

    buffer[0] = info;

    // --- 修改点：改为大端序构造 ID (Big Endian) ---
    // 高位字节放在前面的地址
    buffer[1] = static_cast<std::uint8_t>((id >> 24U) & 0xFFU);
    buffer[2] = static_cast<std::uint8_t>((id >> 16U) & 0xFFU);
    buffer[3] = static_cast<std::uint8_t>((id >> 8U) & 0xFFU);
    buffer[4] = static_cast<std::uint8_t>(id & 0xFFU);

    std::memset(&buffer[5], 0, 8U);
    std::memcpy(&buffer[5], frame.data, dlc);
    return true;
}
