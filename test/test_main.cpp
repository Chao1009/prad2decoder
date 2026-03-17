// test/test_main.cpp
// Usage: evc_test <evio_file> [-v] [-t]
//
// Iterates all events, counts how many times each ROC/slot/channel fires.
// Outputs a JSON array at the end.

#include "EvChannel.h"
#include "Fadc250Data.h"
#include <iostream>
#include <iomanip>
#include <cstdlib>
#include <cstring>
#include <map>

using namespace evc;

int main(int argc, char *argv[])
{
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <evio_file> [-v] [-t]\n";
        return 1;
    }

    const char *filename = argv[1];
    bool verbose = false, tree = false;
    for (int a = 2; a < argc; ++a) {
        if (std::strcmp(argv[a], "-v") == 0) verbose = true;
        else if (std::strcmp(argv[a], "-t") == 0) tree = true;
    }

    EvChannel ch;
    if (ch.Open(filename) != status::success) {
        std::cerr << "Failed to open " << filename << "\n";
        return 1;
    }

    // key = (roc_tag, slot, channel), value = event count
    struct Key {
        uint32_t roc;
        int slot, channel;
        bool operator<(const Key &o) const {
            if (roc != o.roc) return roc < o.roc;
            if (slot != o.slot) return slot < o.slot;
            return channel < o.channel;
        }
    };
    std::map<Key, int> counts;

    fdec::EventData event;
    int total = 0, nread = 0;

    while (ch.Read() == status::success) {
        ++nread;
        if (!ch.Scan()) continue;

        if (tree) {
            auto hdr = ch.GetEvHeader();
            std::cout << "--- Read " << nread
                      << "  tag=0x" << std::hex << hdr.tag << std::dec
                      << "  nevents=" << ch.GetNEvents() << " ---\n";
            ch.PrintTree(std::cout);
        }

        if (ch.GetNEvents() == 0) continue;

        int nevt = ch.GetNEvents();
        for (int i = 0; i < nevt; ++i) {
            if (!ch.DecodeEvent(i, event)) continue;
            ++total;

            for (int r = 0; r < event.nrocs; ++r) {
                auto &roc = event.rocs[r];
                if (!roc.present) continue;

                for (int s = 0; s < fdec::MAX_SLOTS; ++s) {
                    if (!roc.slots[s].present) continue;
                    auto &slot = roc.slots[s];

                    for (int c = 0; c < fdec::MAX_CHANNELS; ++c) {
                        if (!(slot.channel_mask & (1u << c))) continue;

                        if (verbose) {
                            auto &cd = slot.channels[c];
                            std::cout << "ev=" << total
                                      << " ROC=0x" << std::hex << roc.tag << std::dec
                                      << " slot=" << s << " ch=" << c
                                      << " [" << cd.nsamples << "]:";
                            for (int j = 0; j < cd.nsamples; ++j)
                                std::cout << " " << cd.samples[j];
                            std::cout << "\n";
                        }

                        counts[{roc.tag, s, c}]++;
                    }
                }
            }
        }
    }

    // output JSON
    std::cout << "[\n";
    bool first = true;
    for (auto &[k, v] : counts) {
        if (!first) std::cout << ",\n";
        first = false;
        std::cout << "  {\"ROC\": \"0x" << std::hex << k.roc << std::dec
                  << "\", \"slot\": " << k.slot
                  << ", \"channel\": " << k.channel
                  << ", \"events\": " << v << "}";
    }
    std::cout << "\n]\n";

    std::cerr << "Read " << nread << " buffers, " << total << " events, "
              << counts.size() << " channels.\n";
    ch.Close();
    return 0;
}
