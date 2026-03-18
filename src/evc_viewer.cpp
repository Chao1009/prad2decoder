// src/evc_viewer.cpp — HyCal event viewer
//
// Single C++ binary: evio decoder + waveform analysis + HTTP server.
// Auto-discovers database files from compile-time DATABASE_DIR / RESOURCE_DIR.
//
// Usage:
//   evc_viewer <evio_file> [port]
//   evc_viewer <evio_file> [port] --hist                   (use default hist_config.json)
//   evc_viewer <evio_file> [port] --hist my_config.json    (custom config)

#include "EvChannel.h"
#include "Fadc250Data.h"
#include "Fadc250Decoder.h"
#include "WaveAnalyzer.h"

#include <nlohmann/json.hpp>

#include <websocketpp/config/asio_no_tls.hpp>
#include <websocketpp/server.hpp>

#include <fstream>
#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <cstdlib>
#include <cmath>

using json = nlohmann::json;
using WsServer = websocketpp::server<websocketpp::config::asio>;
using namespace evc;

#ifndef DATABASE_DIR
#define DATABASE_DIR "."
#endif
#ifndef RESOURCE_DIR
#define RESOURCE_DIR "."
#endif

// -------------------------------------------------------------------------
// Globals
// -------------------------------------------------------------------------
struct EventIndex { int buffer_num, sub_event; };

static std::string g_filepath;
static std::vector<EventIndex> g_index;
static std::string g_viewer_html;
static json g_config;

// -------------------------------------------------------------------------
// Histogram storage
// -------------------------------------------------------------------------
struct HistConfig {
    float time_min  = 170;      // sample range for peak search
    float time_max  = 190;
    float bin_min   = 0;        // histogram range
    float bin_max   = 20000;
    float bin_step  = 100;
    float threshold = 3.0;      // minimum peak height to count
};

struct Histogram {
    int underflow = 0;
    int overflow  = 0;
    std::vector<int> bins;      // bins.size() = nbins

    void init(int nbins) { bins.assign(nbins, 0); underflow = overflow = 0; }

    void fill(float value, float bin_min, float bin_step)
    {
        if (value < bin_min) { ++underflow; return; }
        int b = static_cast<int>((value - bin_min) / bin_step);
        if (b >= (int)bins.size()) { ++overflow; return; }
        ++bins[b];
    }
};

static HistConfig g_hist_cfg;
static bool g_hist_enabled = false;
// key = "roc_slot_ch", same as event channel keys
static std::map<std::string, Histogram> g_histograms;
static int g_hist_nbins = 0;
static int g_hist_events_processed = 0;

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------
static std::string readFile(const std::string &path)
{
    std::ifstream f(path);
    if (!f) return "";
    return {std::istreambuf_iterator<char>(f), {}};
}

static std::string findFile(const std::string &name, const std::string &base_dir)
{
    { std::ifstream f(name); if (f.good()) return name; }
    std::string p = base_dir + "/" + name;
    { std::ifstream f(p); if (f.good()) return p; }
    return "";
}

// -------------------------------------------------------------------------
// Index the evio file
// -------------------------------------------------------------------------
static void buildIndex(const std::string &path)
{
    g_filepath = path;
    g_index.clear();

    EvChannel ch;
    if (ch.Open(path) != status::success) {
        std::cerr << "Failed to open " << path << "\n";
        return;
    }

    int buf = 0;
    while (ch.Read() == status::success) {
        ++buf;
        if (!ch.Scan()) continue;
        for (int i = 0; i < ch.GetNEvents(); ++i)
            g_index.push_back({buf, i});
    }
    ch.Close();
    std::cerr << "Indexed " << g_index.size() << " events in " << buf << " buffers\n";
}

// -------------------------------------------------------------------------
// Fill a single event's channels into histograms
// -------------------------------------------------------------------------
static void fillHistEvent(fdec::EventData &event, fdec::WaveAnalyzer &ana,
                          fdec::WaveResult &wres)
{
    for (int r = 0; r < event.nrocs; ++r) {
        auto &roc = event.rocs[r];
        if (!roc.present) continue;

        for (int s = 0; s < fdec::MAX_SLOTS; ++s) {
            if (!roc.slots[s].present) continue;
            auto &slot = roc.slots[s];

            for (int c = 0; c < fdec::MAX_CHANNELS; ++c) {
                if (!(slot.channel_mask & (1u << c))) continue;
                auto &cd = slot.channels[c];
                if (cd.nsamples <= 0) continue;

                ana.Analyze(cd.samples, cd.nsamples, wres);

                // find the largest peak within the time range
                float best_integral = -1;
                for (int p = 0; p < wres.npeaks; ++p) {
                    auto &pk = wres.peaks[p];
                    if (pk.height < g_hist_cfg.threshold) continue;
                    if (pk.pos < g_hist_cfg.time_min || pk.pos > g_hist_cfg.time_max) continue;
                    if (pk.integral > best_integral)
                        best_integral = pk.integral;
                }

                if (best_integral < 0) continue;   // no qualifying peak

                std::string key = std::to_string(roc.tag) + "_"
                                + std::to_string(s) + "_" + std::to_string(c);

                auto &h = g_histograms[key];
                if (h.bins.empty()) h.init(g_hist_nbins);
                h.fill(best_integral, g_hist_cfg.bin_min, g_hist_cfg.bin_step);
            }
        }
    }
}

// -------------------------------------------------------------------------
// Build histograms: one full pass over all events
// -------------------------------------------------------------------------
static void buildHistograms()
{
    g_hist_nbins = std::max(1, (int)std::ceil(
        (g_hist_cfg.bin_max - g_hist_cfg.bin_min) / g_hist_cfg.bin_step));
    g_histograms.clear();
    g_hist_events_processed = 0;

    std::cerr << "Building histograms: time [" << g_hist_cfg.time_min << ", "
              << g_hist_cfg.time_max << "], bins " << g_hist_nbins
              << " (" << g_hist_cfg.bin_min << " to " << g_hist_cfg.bin_max
              << ", step " << g_hist_cfg.bin_step << ")\n";

    EvChannel ch;
    if (ch.Open(g_filepath) != status::success) {
        std::cerr << "Failed to open file for histogram pass\n";
        return;
    }

    fdec::EventData event;
    fdec::WaveAnalyzer ana;
    fdec::WaveResult wres;
    int buf = 0, total = 0;

    while (ch.Read() == status::success) {
        ++buf;
        if (!ch.Scan()) continue;
        for (int i = 0; i < ch.GetNEvents(); ++i) {
            if (!ch.DecodeEvent(i, event)) continue;
            fillHistEvent(event, ana, wres);
            ++total;
        }
        // progress
        if (buf % 100 == 0)
            std::cerr << "  " << buf << " buffers, " << total << " events...\r" << std::flush;
    }
    ch.Close();
    g_hist_events_processed = total;

    std::cerr << "Histograms built: " << total << " events, "
              << g_histograms.size() << " channels\n";
}

// -------------------------------------------------------------------------
// Decode one event → JSON (on-demand for viewer)
// -------------------------------------------------------------------------
static json decodeEvent(int ev1)
{
    int idx = ev1 - 1;
    if (idx < 0 || idx >= (int)g_index.size())
        return {{"error", "event out of range"}};

    auto &ei = g_index[idx];
    EvChannel ch;
    if (ch.Open(g_filepath) != status::success)
        return {{"error", "cannot open file"}};

    for (int b = 0; b < ei.buffer_num; ++b)
        if (ch.Read() != status::success) { ch.Close(); return {{"error", "read error"}}; }
    if (!ch.Scan()) { ch.Close(); return {{"error", "scan error"}}; }

    fdec::EventData event;
    if (!ch.DecodeEvent(ei.sub_event, event)) { ch.Close(); return {{"error", "decode error"}}; }
    ch.Close();

    fdec::WaveAnalyzer ana;
    fdec::WaveResult wres;
    json channels = json::object();

    for (int r = 0; r < event.nrocs; ++r) {
        auto &roc = event.rocs[r];
        if (!roc.present) continue;

        for (int s = 0; s < fdec::MAX_SLOTS; ++s) {
            if (!roc.slots[s].present) continue;
            auto &slot = roc.slots[s];

            for (int c = 0; c < fdec::MAX_CHANNELS; ++c) {
                if (!(slot.channel_mask & (1u << c))) continue;
                auto &cd = slot.channels[c];
                if (cd.nsamples <= 0) continue;

                ana.Analyze(cd.samples, cd.nsamples, wres);

                std::string key = std::to_string(roc.tag) + "_"
                                + std::to_string(s) + "_" + std::to_string(c);

                json sarr = json::array();
                for (int j = 0; j < cd.nsamples; ++j) sarr.push_back(cd.samples[j]);

                json parr = json::array();
                for (int p = 0; p < wres.npeaks; ++p) {
                    auto &pk = wres.peaks[p];
                    parr.push_back({
                        {"p", pk.pos}, {"t", std::round(pk.time * 10) / 10},
                        {"h", std::round(pk.height * 10) / 10},
                        {"i", std::round(pk.integral * 10) / 10},
                        {"l", pk.left}, {"r", pk.right},
                        {"o", pk.overflow ? 1 : 0},
                    });
                }

                channels[key] = {
                    {"s", sarr},
                    {"pm", std::round(wres.ped.mean * 10) / 10},
                    {"pr", std::round(wres.ped.rms * 10) / 10},
                    {"pk", parr},
                };
            }
        }
    }
    return {{"event", ev1}, {"channels", channels}};
}

// -------------------------------------------------------------------------
// Histogram API: return one channel's histogram
// -------------------------------------------------------------------------
static json getHistogram(const std::string &key)
{
    if (!g_hist_enabled)
        return {{"error", "histograms not enabled (use --hist)"}};

    auto it = g_histograms.find(key);
    if (it == g_histograms.end())
        return {{"bins", json::array()}, {"underflow", 0}, {"overflow", 0},
                {"events", g_hist_events_processed}};

    auto &h = it->second;
    return {
        {"bins", h.bins},
        {"underflow", h.underflow},
        {"overflow", h.overflow},
        {"events", g_hist_events_processed},
    };
}

// -------------------------------------------------------------------------
// HTTP handler
// -------------------------------------------------------------------------
static void onHttp(WsServer *srv, websocketpp::connection_hdl hdl)
{
    auto con = srv->get_con_from_hdl(hdl);
    std::string uri = con->get_resource();

    if (uri == "/") {
        con->set_status(websocketpp::http::status_code::ok);
        con->set_body(g_viewer_html);
        con->append_header("Content-Type", "text/html; charset=utf-8");
        return;
    }

    if (uri == "/api/config") {
        con->set_status(websocketpp::http::status_code::ok);
        con->set_body(g_config.dump());
        con->append_header("Content-Type", "application/json");
        return;
    }

    // /api/event/<num>
    if (uri.rfind("/api/event/", 0) == 0) {
        int evnum = std::atoi(uri.c_str() + 11);
        con->set_status(websocketpp::http::status_code::ok);
        con->set_body(decodeEvent(evnum).dump());
        con->append_header("Content-Type", "application/json");
        return;
    }

    // /api/hist/<roc>_<slot>_<ch>
    if (uri.rfind("/api/hist/", 0) == 0) {
        std::string key = uri.substr(10);
        con->set_status(websocketpp::http::status_code::ok);
        con->set_body(getHistogram(key).dump());
        con->append_header("Content-Type", "application/json");
        return;
    }

    con->set_status(websocketpp::http::status_code::not_found);
    con->set_body("404 Not Found");
}

// -------------------------------------------------------------------------
// Main
// -------------------------------------------------------------------------
int main(int argc, char *argv[])
{
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <evio_file> [port] [--hist [config.json]]\n";
        return 1;
    }

    std::string evio_file = argv[1];
    int port = 5050;
    std::string hist_config_file;

    // parse args
    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--hist") {
            g_hist_enabled = true;
            // optional: next arg is config file if it doesn't start with '-'
            if (i + 1 < argc && argv[i+1][0] != '-')
                hist_config_file = argv[++i];
        } else if (arg[0] != '-') {
            port = std::atoi(argv[i]);
        }
    }

    // auto-discover paths
    std::string db_dir  = DATABASE_DIR;
    std::string res_dir = RESOURCE_DIR;

    // load histogram config
    if (g_hist_enabled) {
        if (hist_config_file.empty())
            hist_config_file = findFile("hist_config.json", db_dir);

        std::string hcfg_str = readFile(hist_config_file);
        if (!hcfg_str.empty()) {
            auto hcfg = json::parse(hcfg_str, nullptr, false);
            if (hcfg.contains("hist")) {
                auto &h = hcfg["hist"];
                if (h.contains("time_min"))  g_hist_cfg.time_min  = h["time_min"];
                if (h.contains("time_max"))  g_hist_cfg.time_max  = h["time_max"];
                if (h.contains("bin_min"))   g_hist_cfg.bin_min   = h["bin_min"];
                if (h.contains("bin_max"))   g_hist_cfg.bin_max   = h["bin_max"];
                if (h.contains("bin_step"))  g_hist_cfg.bin_step  = h["bin_step"];
                if (h.contains("threshold")) g_hist_cfg.threshold = h["threshold"];
            }
            std::cerr << "Hist config: " << hist_config_file << "\n";
        } else {
            std::cerr << "Hist config: using defaults (no config file found)\n";
        }
    }

    // index events
    buildIndex(evio_file);

    // build histograms if enabled (one full pass)
    if (g_hist_enabled)
        buildHistograms();

    // load static files
    std::string html_file = findFile("viewer.html", res_dir);
    std::string mod_file  = findFile("hycal_modules.json", db_dir);
    std::string daq_file  = findFile("daq_map.json", db_dir);

    g_viewer_html = readFile(html_file);
    if (g_viewer_html.empty())
        std::cerr << "Warning: viewer.html not found (tried " << res_dir << ")\n";

    // build config JSON
    json modules_j = json::array(), daq_j = json::array();
    { std::string s = readFile(mod_file);  if (!s.empty()) modules_j = json::parse(s, nullptr, false); }
    { std::string s = readFile(daq_file);  if (!s.empty()) daq_j     = json::parse(s, nullptr, false); }

    g_config = {
        {"modules", modules_j},
        {"daq", daq_j},
        {"crate_roc", {{"0",0x80},{"1",0x82},{"2",0x84},{"3",0x86},{"4",0x88},{"5",0x8a},{"6",0x8c}}},
        {"total_events", (int)g_index.size()},
        {"hist_enabled", g_hist_enabled},
    };
    if (g_hist_enabled) {
        g_config["hist"] = {
            {"time_min",  g_hist_cfg.time_min},
            {"time_max",  g_hist_cfg.time_max},
            {"bin_min",   g_hist_cfg.bin_min},
            {"bin_max",   g_hist_cfg.bin_max},
            {"bin_step",  g_hist_cfg.bin_step},
            {"threshold", g_hist_cfg.threshold},
            {"events",    g_hist_events_processed},
        };
    }

    std::cerr << "Database  : " << db_dir << " ("
              << modules_j.size() << " modules, " << daq_j.size() << " DAQ channels)\n"
              << "Resources : " << res_dir << "\n";

    // start server
    WsServer server;
    server.set_access_channels(websocketpp::log::alevel::none);
    server.set_error_channels(websocketpp::log::elevel::warn | websocketpp::log::elevel::rerror);
    server.init_asio();
    server.set_reuse_addr(true);
    server.set_http_handler([&server](websocketpp::connection_hdl hdl) { onHttp(&server, hdl); });
    server.listen(port);
    server.start_accept();

    std::cout << "Viewer at http://localhost:" << port << "\n"
              << "  " << g_index.size() << " events"
              << (g_hist_enabled ? ", histograms enabled" : "") << "\n"
              << "  Ctrl+C to stop\n";

    server.run();
    return 0;
}
