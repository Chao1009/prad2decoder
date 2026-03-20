#include "Fadc250Decoder.h"
#include <cstring>
#include <iostream>

using namespace fdec;

int Fadc250Decoder::DecodeRoc(const uint8_t *data, size_t nbytes, RocData &roc)
{
    if (!data || !nbytes) return 0;

    int nslots = 0;
    size_t pos = 0;

    while (pos + 17 <= nbytes) {
        // slot header: c(1) + i(4) + l(8) + N(4) = 17 bytes
        uint8_t  slot_id = data[pos]; pos += 1;
        int32_t  trigger;   std::memcpy(&trigger,   data + pos, 4); pos += 4;
        int64_t  timestamp; std::memcpy(&timestamp, data + pos, 8); pos += 8;
        uint32_t nchan;     std::memcpy(&nchan,     data + pos, 4); pos += 4;

        if (slot_id >= MAX_SLOTS) {
            std::cerr << "Fadc250Decoder: slot_id=" << (int)slot_id
                      << " >= MAX_SLOTS=" << MAX_SLOTS << "\n";
            break;
        }
        if (nchan > MAX_CHANNELS) {
            std::cerr << "Fadc250Decoder: nchan=" << nchan << " at slot=" << (int)slot_id << "\n";
            break;
        }

        SlotData &s = roc.slots[slot_id];
        s.present   = true;
        s.trigger   = trigger;
        s.timestamp = timestamp;
        s.nchannels = 0;
        s.channel_mask = 0;

        for (uint32_t i = 0; i < nchan; ++i) {
            if (pos + 5 > nbytes) break;
            uint8_t ch = data[pos]; pos += 1;

            uint32_t nsamp;
            std::memcpy(&nsamp, data + pos, 4); pos += 4;

            if (ch >= MAX_CHANNELS || nsamp > MAX_SAMPLES) {
                std::cerr << "Fadc250Decoder: bad ch=" << (int)ch
                          << " nsamp=" << nsamp << "\n";
                return nslots;
            }

            size_t bytes = size_t(nsamp) * 2;
            if (pos + bytes > nbytes) { nsamp = (nbytes - pos) / 2; bytes = nsamp * 2; }

            // copy samples directly (native LE uint16)
            ChannelData &cd = s.channels[ch];
            cd.nsamples = nsamp;
            std::memcpy(cd.samples, data + pos, bytes);
            pos += bytes;

            s.channel_mask |= (1ull << ch);
            s.nchannels++;
        }
        nslots++;
    }

    roc.nslots = nslots;
    return nslots;
}
