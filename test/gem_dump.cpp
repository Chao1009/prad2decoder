// test/gem_dump.cpp — GEM data diagnostic tool
//
// Decodes SSP/MPD banks from EVIO data, optionally runs GEM reconstruction,
// and prints diagnostic output at each stage of the pipeline.
//
// Usage:
//   gem_dump <evio_file> -D <daq_config.json> [options]
//
// Modes (default: summary):
//   -m raw        Dump raw SSP-decoded APV data (strips × time samples)
//   -m hits       Process through GemSystem → show strip hits per plane
//   -m clusters   Full reconstruction → show clusters and 2D GEM hits
//   -m summary    Statistics: MPDs, APVs, strips, hits, clusters per event
//
// Options:
//   -D <file>     DAQ configuration (required)
//   -G <file>     GEM map file (default: gem_map.json from DAQ config dir)
//   -P <file>     GEM pedestal file (optional, required for good hit finding)
//   -n <N>        Max physics events to process (default: 10, 0=all)
//   -t <bit>      Trigger bit filter (default: -1 = accept all)
//   -e <N>        Dump only event N (1-based physics event number)

#include "EvChannel.h"
#include "Fadc250Data.h"
#include "SspData.h"
#include "load_daq_config.h"
#include "GemSystem.h"
#include "GemCluster.h"

#include <nlohmann/json.hpp>
#include <iostream>
#include <iomanip>
#include <string>
#include <map>
#include <cstdlib>
#include <getopt.h>
#include <memory>

using namespace evc;

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------
static std::string hex(uint32_t v)
{
    char buf[16];
    snprintf(buf, sizeof(buf), "0x%04X", v);
    return buf;
}

// -------------------------------------------------------------------------
// Mode: raw — dump decoded SSP APV data
// -------------------------------------------------------------------------
static void dumpRawSsp(const ssp::SspEventData &ssp, int phys_ev)
{
    std::cout << "--- Event " << phys_ev << ": "
              << ssp.nmpds << " MPD(s) ---\n";

    for (int m = 0; m < ssp.nmpds; ++m) {
        auto &mpd = ssp.mpds[m];
        if (!mpd.present) continue;

        for (int a = 0; a < ssp::MAX_APVS_PER_MPD; ++a) {
            auto &apv = mpd.apvs[a];
            if (!apv.present) continue;

            std::cout << "  MPD crate=" << mpd.crate_id
                      << " fiber=" << mpd.mpd_id
                      << " APV=" << a
                      << " strips=" << apv.nstrips;
            if (apv.has_online_cm) {
                std::cout << " online_cm=[";
                for (int t = 0; t < ssp::SSP_TIME_SAMPLES; ++t) {
                    if (t) std::cout << ",";
                    std::cout << apv.online_cm[t];
                }
                std::cout << "]";
            }
            std::cout << "\n";

            // print first 8 strips with data (or all if ≤ 8)
            int printed = 0;
            for (int s = 0; s < ssp::APV_STRIP_SIZE && printed < 8; ++s) {
                if (!apv.hasStrip(s)) continue;
                printed++;
                std::cout << "    ch[" << std::setw(3) << s << "] =";
                for (int t = 0; t < ssp::SSP_TIME_SAMPLES; ++t)
                    std::cout << " " << std::setw(6) << apv.strips[s][t];
                std::cout << "\n";
            }
            if (apv.nstrips > 8)
                std::cout << "    ... (" << apv.nstrips - 8 << " more strips)\n";
        }
    }
    std::cout << "\n";
}

// -------------------------------------------------------------------------
// Mode: hits — show strip hits after GemSystem processing
// -------------------------------------------------------------------------
static void dumpHits(gem::GemSystem &sys, int phys_ev)
{
    int total_hits = 0;
    for (int d = 0; d < sys.GetNDetectors(); ++d)
        for (int p = 0; p < 2; ++p)
            total_hits += (int)sys.GetPlaneHits(d, p).size();

    std::cout << "--- Event " << phys_ev << ": "
              << total_hits << " strip hit(s) ---\n";

    for (int d = 0; d < sys.GetNDetectors(); ++d) {
        auto &det = sys.GetDetectors()[d];
        for (int p = 0; p < 2; ++p) {
            auto &hits = sys.GetPlaneHits(d, p);
            if (hits.empty()) continue;

            std::cout << "  " << det.name << " " << (p == 0 ? "X" : "Y")
                      << ": " << hits.size() << " hit(s)\n";

            int show = std::min((int)hits.size(), 12);
            for (int i = 0; i < show; ++i) {
                auto &h = hits[i];
                std::cout << "    strip=" << std::setw(4) << h.strip
                          << " pos=" << std::fixed << std::setprecision(2)
                          << std::setw(8) << h.position << "mm"
                          << " charge=" << std::setw(8) << std::setprecision(1)
                          << h.charge
                          << " tbin=" << h.max_timebin;
                if (h.cross_talk) std::cout << " [xtalk]";
                // show time samples
                if (!h.ts_adc.empty()) {
                    std::cout << "  ts=[";
                    for (size_t t = 0; t < h.ts_adc.size(); ++t) {
                        if (t) std::cout << ",";
                        std::cout << std::setprecision(0) << h.ts_adc[t];
                    }
                    std::cout << "]";
                }
                std::cout << "\n";
            }
            if ((int)hits.size() > show)
                std::cout << "    ... (" << hits.size() - show << " more)\n";
        }
    }
    std::cout << "\n";
}

// -------------------------------------------------------------------------
// Mode: clusters — show clusters and 2D reconstructed hits
// -------------------------------------------------------------------------
static void dumpClusters(gem::GemSystem &sys, int phys_ev)
{
    auto &all_hits = sys.GetAllHits();
    std::cout << "--- Event " << phys_ev << ": "
              << all_hits.size() << " reconstructed 2D hit(s) ---\n";

    for (int d = 0; d < sys.GetNDetectors(); ++d) {
        auto &det = sys.GetDetectors()[d];

        // show 1D clusters per plane
        for (int p = 0; p < 2; ++p) {
            auto &clusters = sys.GetPlaneClusters(d, p);
            if (clusters.empty()) continue;

            std::cout << "  " << det.name << " " << (p == 0 ? "X" : "Y")
                      << ": " << clusters.size() << " cluster(s)\n";

            int show = std::min((int)clusters.size(), 8);
            for (int i = 0; i < show; ++i) {
                auto &cl = clusters[i];
                std::cout << "    pos=" << std::fixed << std::setprecision(2)
                          << std::setw(8) << cl.position << "mm"
                          << " peak=" << std::setprecision(1) << std::setw(8) << cl.peak_charge
                          << " total=" << std::setw(8) << cl.total_charge
                          << " size=" << cl.hits.size()
                          << " tbin=" << cl.max_timebin;
                if (cl.cross_talk) std::cout << " [xtalk]";
                std::cout << "\n";
            }
            if ((int)clusters.size() > show)
                std::cout << "    ... (" << clusters.size() - show << " more)\n";
        }

        // show 2D hits
        auto &hits2d = sys.GetHits(d);
        if (!hits2d.empty()) {
            std::cout << "  " << det.name << " 2D hits: " << hits2d.size() << "\n";
            int show = std::min((int)hits2d.size(), 8);
            for (int i = 0; i < show; ++i) {
                auto &h = hits2d[i];
                std::cout << "    (" << std::fixed << std::setprecision(2)
                          << std::setw(8) << h.x << ", "
                          << std::setw(8) << h.y << ") mm"
                          << "  Qx=" << std::setprecision(0) << h.x_charge
                          << " Qy=" << h.y_charge
                          << " Nx=" << h.x_size << " Ny=" << h.y_size
                          << "\n";
            }
            if ((int)hits2d.size() > show)
                std::cout << "    ... (" << hits2d.size() - show << " more)\n";
        }
    }
    std::cout << "\n";
}

// -------------------------------------------------------------------------
// Summary accumulator
// -------------------------------------------------------------------------
struct EventStats {
    int nmpds       = 0;
    int napvs       = 0;
    int nstrips     = 0;
    int nhits_x     = 0;
    int nhits_y     = 0;
    int nclusters_x = 0;
    int nclusters_y = 0;
    int nhits_2d    = 0;
};

static void accumulateStats(const ssp::SspEventData &ssp,
                            gem::GemSystem *sys,
                            EventStats &st)
{
    for (int m = 0; m < ssp.nmpds; ++m) {
        if (!ssp.mpds[m].present) continue;
        st.nmpds++;
        for (int a = 0; a < ssp::MAX_APVS_PER_MPD; ++a) {
            if (!ssp.mpds[m].apvs[a].present) continue;
            st.napvs++;
            st.nstrips += ssp.mpds[m].apvs[a].nstrips;
        }
    }

    if (sys) {
        for (int d = 0; d < sys->GetNDetectors(); ++d) {
            st.nhits_x     += (int)sys->GetPlaneHits(d, 0).size();
            st.nhits_y     += (int)sys->GetPlaneHits(d, 1).size();
            st.nclusters_x += (int)sys->GetPlaneClusters(d, 0).size();
            st.nclusters_y += (int)sys->GetPlaneClusters(d, 1).size();
            st.nhits_2d    += (int)sys->GetHits(d).size();
        }
    }
}

// -------------------------------------------------------------------------
// Main
// -------------------------------------------------------------------------
static void usage(const char *prog)
{
    std::cerr
        << "GEM data diagnostic tool\n\n"
        << "Usage:\n"
        << "  " << prog << " <evio_file> -D <daq_config.json> [options]\n\n"
        << "Modes (default: summary):\n"
        << "  -m raw        Dump raw SSP-decoded APV data\n"
        << "  -m hits       Strip hits after pedestal/CM/zero-sup\n"
        << "  -m clusters   Full reconstruction: clusters + 2D hits\n"
        << "  -m summary    Per-event statistics table\n\n"
        << "Options:\n"
        << "  -D <file>     DAQ configuration (required)\n"
        << "  -G <file>     GEM map file (default: gem_map.json)\n"
        << "  -P <file>     GEM pedestal file\n"
        << "  -n <N>        Max physics events (default: 10, 0=all)\n"
        << "  -t <bit>      Trigger bit filter (-1=all, default)\n"
        << "  -e <N>        Dump only physics event N (1-based)\n";
}

int main(int argc, char *argv[])
{
    if (argc < 2) { usage(argv[0]); return 1; }

    std::string daq_config_file;
    std::string gem_map_file;
    std::string gem_ped_file;
    std::string mode = "summary";
    int max_events  = 10;
    int trigger_bit = -1;   // -1 = accept all
    int target_event = 0;   // 0 = disabled

    int opt;
    while ((opt = getopt(argc, argv, "D:G:P:m:n:t:e:h")) != -1) {
        switch (opt) {
        case 'D': daq_config_file = optarg; break;
        case 'G': gem_map_file = optarg; break;
        case 'P': gem_ped_file = optarg; break;
        case 'm': mode = optarg; break;
        case 'n': max_events = std::atoi(optarg); break;
        case 't': trigger_bit = std::atoi(optarg); break;
        case 'e': target_event = std::atoi(optarg); max_events = 0; break;
        default:  usage(argv[0]); return 1;
        }
    }
    if (optind >= argc) { usage(argv[0]); return 1; }
    std::string evio_file = argv[optind];

    // validate mode
    bool need_gem = (mode == "hits" || mode == "clusters" || mode == "summary");
    bool need_cluster = (mode == "clusters" || mode == "summary");

    // load DAQ config
    if (daq_config_file.empty()) {
        std::cerr << "Error: -D <daq_config.json> is required\n";
        return 1;
    }
    DaqConfig daq_cfg;
    if (!load_daq_config(daq_config_file, daq_cfg)) {
        std::cerr << "Error: failed to load DAQ config: " << daq_config_file << "\n";
        return 1;
    }
    std::cerr << "DAQ config: " << daq_config_file
              << " (adc_format=" << daq_cfg.adc_format << ")\n";

    // resolve GEM map file (default: gem_map.json next to DAQ config)
    if (gem_map_file.empty() && need_gem) {
        // try same directory as daq config
        auto pos = daq_config_file.rfind('/');
        if (pos == std::string::npos) pos = daq_config_file.rfind('\\');
        std::string dir = (pos != std::string::npos) ? daq_config_file.substr(0, pos + 1) : "";
        gem_map_file = dir + "gem_map.json";
    }

    // initialize GEM system
    std::unique_ptr<gem::GemSystem> gem_sys;
    std::unique_ptr<gem::GemCluster> gem_clusterer;

    if (need_gem) {
        gem_sys = std::make_unique<gem::GemSystem>();
        gem_sys->Init(gem_map_file);
        std::cerr << "GEM map  : " << gem_map_file
                  << " (" << gem_sys->GetNDetectors() << " detectors)\n";

        if (!gem_ped_file.empty()) {
            gem_sys->LoadPedestals(gem_ped_file);
            std::cerr << "GEM peds : " << gem_ped_file << "\n";
        }

        if (need_cluster)
            gem_clusterer = std::make_unique<gem::GemCluster>();
    }

    // open EVIO file
    EvChannel ch;
    ch.SetConfig(daq_cfg);
    if (ch.Open(evio_file) != status::success) {
        std::cerr << "Error: cannot open " << evio_file << "\n";
        return 1;
    }
    std::cerr << "File     : " << evio_file << "\n\n";

    // trigger filter
    uint32_t trigger_mask = 0;
    if (trigger_bit >= 0) {
        trigger_mask = 1u << trigger_bit;
        std::cerr << "Trigger  : bit " << trigger_bit
                  << " (mask 0x" << std::hex << trigger_mask << std::dec << ")\n";
    }

    // summary mode header
    if (mode == "summary") {
        std::cout << std::setw(6) << "ev#"
                  << std::setw(10) << "trigger#"
                  << std::setw(6) << "MPDs"
                  << std::setw(6) << "APVs"
                  << std::setw(8) << "strips"
                  << std::setw(8) << "hits_X"
                  << std::setw(8) << "hits_Y"
                  << std::setw(8) << "clus_X"
                  << std::setw(8) << "clus_Y"
                  << std::setw(8) << "2D_hits"
                  << "\n";
        std::cout << std::string(76, '-') << "\n";
    }

    // event loop
    auto event_ptr = std::make_unique<fdec::EventData>();
    auto ssp_ptr   = std::make_unique<ssp::SspEventData>();
    auto &event    = *event_ptr;
    auto &ssp_evt  = *ssp_ptr;

    int phys_count = 0;
    int ssp_events = 0;

    // totals for summary
    EventStats totals;

    while (ch.Read() == status::success) {
        if (!ch.Scan()) continue;
        if (ch.GetEventType() != EventType::Physics) continue;

        for (int i = 0; i < ch.GetNEvents(); ++i) {
            ssp_evt.clear();
            if (!ch.DecodeEvent(i, event, &ssp_evt)) continue;
            phys_count++;

            // trigger filter
            if (trigger_mask && !(event.info.trigger_bits & trigger_mask))
                continue;

            // target event filter
            if (target_event > 0 && phys_count != target_event)
                continue;

            // skip events with no SSP data
            if (ssp_evt.nmpds == 0) {
                if (target_event > 0) {
                    std::cout << "Event " << phys_count << ": no SSP data\n";
                    goto done;
                }
                continue;
            }

            ssp_events++;

            // GEM processing
            if (gem_sys) {
                gem_sys->Clear();
                gem_sys->ProcessEvent(ssp_evt);
                if (gem_clusterer)
                    gem_sys->Reconstruct(*gem_clusterer);
            }

            // output based on mode
            if (mode == "raw") {
                std::cout << "[physics event " << phys_count
                          << " trigger#=" << event.info.trigger_number
                          << " bits=0x" << std::hex << event.info.trigger_bits
                          << std::dec << "]\n";
                dumpRawSsp(ssp_evt, phys_count);
            }
            else if (mode == "hits") {
                std::cout << "[physics event " << phys_count
                          << " trigger#=" << event.info.trigger_number
                          << " bits=0x" << std::hex << event.info.trigger_bits
                          << std::dec << "]\n";
                dumpHits(*gem_sys, phys_count);
            }
            else if (mode == "clusters") {
                std::cout << "[physics event " << phys_count
                          << " trigger#=" << event.info.trigger_number
                          << " bits=0x" << std::hex << event.info.trigger_bits
                          << std::dec << "]\n";
                dumpClusters(*gem_sys, phys_count);
            }
            else { // summary
                EventStats st;
                accumulateStats(ssp_evt, gem_sys.get(), st);

                std::cout << std::setw(6) << phys_count
                          << std::setw(10) << event.info.trigger_number
                          << std::setw(6) << st.nmpds
                          << std::setw(6) << st.napvs
                          << std::setw(8) << st.nstrips
                          << std::setw(8) << st.nhits_x
                          << std::setw(8) << st.nhits_y
                          << std::setw(8) << st.nclusters_x
                          << std::setw(8) << st.nclusters_y
                          << std::setw(8) << st.nhits_2d
                          << "\n";

                // accumulate totals
                totals.nmpds       += st.nmpds;
                totals.napvs       += st.napvs;
                totals.nstrips     += st.nstrips;
                totals.nhits_x     += st.nhits_x;
                totals.nhits_y     += st.nhits_y;
                totals.nclusters_x += st.nclusters_x;
                totals.nclusters_y += st.nclusters_y;
                totals.nhits_2d    += st.nhits_2d;
            }

            if (target_event > 0)
                goto done;
            if (max_events > 0 && ssp_events >= max_events)
                goto done;
        }
        if (max_events > 0 && ssp_events >= max_events)
            break;
    }

done:
    ch.Close();

    // summary footer
    if (mode == "summary" && ssp_events > 0) {
        std::cout << std::string(76, '-') << "\n";
        std::cout << "Totals: " << ssp_events << " events with SSP data"
                  << " (of " << phys_count << " physics events)\n";
        if (ssp_events > 0) {
            std::cout << "  Avg per event:"
                      << " MPDs=" << std::fixed << std::setprecision(1)
                      << (float)totals.nmpds / ssp_events
                      << " APVs=" << (float)totals.napvs / ssp_events
                      << " strips=" << (float)totals.nstrips / ssp_events
                      << "\n";
            if (gem_sys) {
                std::cout << "  Avg per event:"
                          << " hits_X=" << (float)totals.nhits_x / ssp_events
                          << " hits_Y=" << (float)totals.nhits_y / ssp_events
                          << " clus_X=" << (float)totals.nclusters_x / ssp_events
                          << " clus_Y=" << (float)totals.nclusters_y / ssp_events
                          << " 2D=" << (float)totals.nhits_2d / ssp_events
                          << "\n";
            }
        }
    }
    else if (ssp_events == 0) {
        std::cerr << "No events with SSP data found in " << phys_count
                  << " physics events.\n";
    }

    std::cerr << "Done: " << phys_count << " physics events, "
              << ssp_events << " with SSP data.\n";
    return 0;
}
