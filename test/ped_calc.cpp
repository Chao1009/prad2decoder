// test/ped_calc.cpp — compute per-channel pedestals from EVIO data
//
// Iterates all events matching a given trigger bit, accumulates raw ADC
// values per (crate, slot, channel), and outputs mean + rms as JSON.
//
// Usage:
//   ped_calc <evio_file> [-D <daq_config.json>] [-t <trigger_bit>] [-o <output.json>] [-n <max_events>]
//
// Options:
//   -D   DAQ configuration file (auto-searches daq_config.json if omitted)
//   -t   Trigger bit to select (default: 3, i.e. 0x08 = LMS_Alpha for PRad)
//   -o   Output JSON file (default: pedestals_out.json)
//   -n   Max events to process (default: all)
//
// The trigger bit is the bit position (0-based), so:
//   bit 0 → 0x01 (PHYS_LeadGlassSum)
//   bit 1 → 0x02 (PHYS_TotalSum)
//   bit 2 → 0x04 (LMS_Led)
//   bit 3 → 0x08 (LMS_Alpha / pedestal)

#include "EvChannel.h"
#include "Fadc250Data.h"
#include "load_daq_config.h"

#include <nlohmann/json.hpp>
#include <iostream>
#include <fstream>
#include <string>
#include <map>
#include <cmath>
#include <cstdlib>
#include <memory>

using namespace evc;
using json = nlohmann::json;

struct ChannelAccum {
    int    crate   = 0;
    int    slot    = 0;
    int    channel = 0;
    double sum     = 0.;
    double sum2    = 0.;
    int    count   = 0;

    void add(double val) {
        sum  += val;
        sum2 += val * val;
        count++;
    }

    double mean() const { return count > 0 ? sum / count : 0.; }
    double rms()  const {
        if (count < 2) return 0.;
        double m = mean();
        double var = sum2 / count - m * m;
        return var > 0 ? std::sqrt(var) : 0.;
    }
};

static void usage(const char *prog)
{
    std::cerr
        << "Compute per-channel pedestals from EVIO data\n\n"
        << "Usage:\n"
        << "  " << prog << " <evio_file> -D <daq_config.json> [options]\n\n"
        << "Options:\n"
        << "  -D <file>   DAQ configuration (auto-searches daq_config.json if omitted)\n"
        << "  -t <bit>    Trigger bit to select (default: 3 = 0x08)\n"
        << "  -o <file>   Output JSON file (default: pedestals_out.json)\n"
        << "  -n <N>      Max events to process (default: all)\n\n"
        << "Trigger bits (PRad):\n"
        << "  0 = PHYS_LeadGlassSum (0x01)\n"
        << "  1 = PHYS_TotalSum     (0x02)\n"
        << "  2 = LMS_Led           (0x04)\n"
        << "  3 = LMS_Alpha         (0x08)\n";
}

int main(int argc, char *argv[])
{
    if (argc < 2) { usage(argv[0]); return 1; }

    std::string evio_file;
    std::string daq_config_file;
    std::string output_file = "pedestals_out.json";
    int trigger_bit = 3;  // default: LMS_Alpha (0x08)
    int max_events  = 0;  // 0 = all

    // parse args
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "-D" && i + 1 < argc) { daq_config_file = argv[++i]; }
        else if (a == "-t" && i + 1 < argc) { trigger_bit = std::atoi(argv[++i]); }
        else if (a == "-o" && i + 1 < argc) { output_file = argv[++i]; }
        else if (a == "-n" && i + 1 < argc) { max_events = std::atoi(argv[++i]); }
        else if (evio_file.empty()) { evio_file = a; }
    }

    if (evio_file.empty()) { usage(argv[0]); return 1; }

    uint32_t trigger_mask = 1u << trigger_bit;
    std::cerr << "Input    : " << evio_file << "\n"
              << "Output   : " << output_file << "\n"
              << "Trigger  : bit " << trigger_bit << " (mask 0x"
              << std::hex << trigger_mask << std::dec << ")\n";

    // auto-search for daq_config.json if not specified
    if (daq_config_file.empty()) {
        for (auto p : {"daq_config.json", "database/daq_config.json", "../database/daq_config.json"}) {
            std::ifstream f(p);
            if (f.good()) { daq_config_file = p; break; }
        }
    }

    // load DAQ config
    DaqConfig daq_cfg;
    if (daq_config_file.empty() || !load_daq_config(daq_config_file, daq_cfg)) {
        std::cerr << "Error: failed to load DAQ config"
                  << (daq_config_file.empty() ? " (not found)" : ": " + daq_config_file)
                  << "\n";
        return 1;
    }
    std::cerr << "DAQ cfg  : " << daq_config_file
              << " (adc_format=" << daq_cfg.adc_format << ")\n";

    // build ROC tag → crate map
    std::map<uint32_t, int> roc_to_crate;
    for (auto &re : daq_cfg.roc_tags)
        if (re.crate >= 0)
            roc_to_crate[re.tag] = re.crate;

    // open file
    EvChannel ch;
    ch.SetConfig(daq_cfg);
    if (ch.Open(evio_file) != status::success) {
        std::cerr << "Error: cannot open " << evio_file << "\n";
        return 1;
    }

    // accumulate: key = (crate, slot, channel)
    using Key = uint64_t;
    auto make_key = [](int crate, int slot, int ch) -> Key {
        return (static_cast<Key>(crate) << 32) |
               (static_cast<Key>(slot) << 16) |
               static_cast<Key>(ch);
    };
    std::map<Key, ChannelAccum> accum;

    auto event_ptr = std::make_unique<fdec::EventData>();
    auto &event = *event_ptr;
    int total = 0, selected = 0;

    while (ch.Read() == status::success) {
        if (!ch.Scan()) continue;
        if (ch.GetEventType() != EventType::Physics) continue;

        for (int i = 0; i < ch.GetNEvents(); ++i) {
            if (!ch.DecodeEvent(i, event)) continue;
            total++;

            // check trigger bit
            if (!(event.info.trigger_bits & trigger_mask)) continue;
            selected++;

            // accumulate raw ADC values (no pedestal subtraction)
            for (int r = 0; r < event.nrocs; ++r) {
                auto &roc = event.rocs[r];
                if (!roc.present) continue;
                auto cit = roc_to_crate.find(roc.tag);
                if (cit == roc_to_crate.end()) continue;
                int crate = cit->second;

                for (int s = 0; s < fdec::MAX_SLOTS; ++s) {
                    if (!roc.slots[s].present) continue;
                    auto &slot = roc.slots[s];
                    for (int c = 0; c < fdec::MAX_CHANNELS; ++c) {
                        if (!(slot.channel_mask & (1ull << c))) continue;
                        auto &cd = slot.channels[c];
                        if (cd.nsamples <= 0) continue;

                        Key k = make_key(crate, s, c);
                        auto &acc = accum[k];
                        if (acc.count == 0) {
                            acc.crate   = crate;
                            acc.slot    = s;
                            acc.channel = c;
                        }
                        // use raw sample value (before pedestal subtraction)
                        acc.add(cd.samples[0]);
                    }
                }
            }

            if (max_events > 0 && selected >= max_events) break;
        }
        if (max_events > 0 && selected >= max_events) break;

        // progress
        if (total % 5000 == 0)
            std::cerr << "  " << total << " events scanned, "
                      << selected << " selected...\r" << std::flush;
    }
    ch.Close();

    std::cerr << "\nDone: " << total << " physics events, "
              << selected << " matched trigger bit " << trigger_bit
              << ", " << accum.size() << " channels\n";

    // write output JSON
    json out = json::array();
    for (auto &[k, acc] : accum) {
        out.push_back({
            {"crate",   acc.crate},
            {"slot",    acc.slot},
            {"channel", acc.channel},
            {"mean",    std::round(acc.mean() * 1000.) / 1000.},
            {"rms",     std::round(acc.rms() * 10000.) / 10000.},
            {"count",   acc.count},
        });
    }

    std::ofstream of(output_file);
    if (!of.is_open()) {
        std::cerr << "Error: cannot write " << output_file << "\n";
        return 1;
    }
    of << out.dump(2) << "\n";
    of.close();

    std::cerr << "Written: " << output_file << " (" << out.size() << " channels)\n";
    return 0;
}
