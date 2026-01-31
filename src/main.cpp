#include "bridge.hpp"
#include "config.hpp"

#include <csignal>
#include <cstdio>
#include <cstring>
#include <string>
#include <atomic>

namespace {

std::atomic<bool> g_keep_running(true);

void signal_handler(int) {
    g_keep_running.store(false);
}

void print_usage(const char *prog) {
    std::fprintf(stderr, "Usage: %s --config <path>\n", prog);
}

} // namespace

int main(int argc, char **argv) {
    std::string config_path;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--config") == 0 && (i + 1) < argc) {
            config_path = argv[++i];
        } else {
            print_usage(argv[0]);
            return 1;
        }
    }

    if (config_path.empty()) {
        config_path = "config/minimal_config.json";
    }

    BridgeConfig config{};
    std::string error_message;
    if (!load_bridge_config(config_path, config, error_message)) {
        std::fprintf(stderr, "config error: %s\n", error_message.c_str());
        return 1;
    }

    if (config.ports.empty()) {
        std::fprintf(stderr, "config error: at least one port entry is required\n");
        return 1;
    }
    const PortConfig &port = config.ports.front();
    if (port.channels.empty()) {
        std::fprintf(stderr, "config error: port definition must include at least one channel\n");
        return 1;
    }
    const ChannelConfig &channel = port.channels.front();

    BridgeApp app(config.server, port, channel);
    if (!app.initialize()) {
        return 1;
    }

    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    std::printf("Bridge is running. Press Ctrl+C to stop.\n");
    app.run(g_keep_running);
    std::printf("Shutting down...\n");
    return 0;
}
