#include "config.hpp"

#include <cstdio>
#include <cstring>
#include <string>

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "Usage: %s <config.json>\n", argv[0]);
        return 1;
    }

    BridgeConfig config{};
    std::string error;
    if (!load_bridge_config(argv[1], config, error)) {
        std::fprintf(stderr, "Config invalid: %s\n", error.c_str());
        return 1;
    }

    std::printf("Config OK: server=%s listen_ports=%zu\n",
                config.server.ip.c_str(),
                config.ports.size());
    return 0;
}
