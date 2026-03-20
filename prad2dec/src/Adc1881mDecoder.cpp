#include "Adc1881mDecoder.h"
#include <iostream>

using namespace fdec;

int Adc1881mDecoder::DecodeRoc(const uint32_t *data, size_t nwords, RocData &roc)
{
    if (!data || nwords < 2) return 0;

    // Validate self-defined crate data header
    if ((data[0] & DATA_BEGIN_MASK) != DATA_BEGIN) {
        std::cerr << "Adc1881mDecoder: bad header 0x" << std::hex << data[0]
                  << std::dec << "\n";
        return -1;
    }

    // Number of boards encoded in low byte of header
    unsigned int board_count = data[0] & 0xFF;
    int nslots = 0;
    size_t idx = 1;

    for (unsigned int b = 0; b < board_count && idx < nwords; ++b) {
        // Skip 64-bit alignment word
        if (data[idx] == ALIGNMENT) {
            ++idx;
            if (idx >= nwords) break;
        }
        // Check for end-of-crate marker
        if (data[idx] == DATA_END) break;

        // Board header word: slot in bits[31:27], word count in bits[6:0]
        uint32_t slot_id = (data[idx] >> 27) & 0x1F;
        unsigned int word_end = (data[idx] & 0x7F) + idx;

        if (slot_id >= MAX_SLOTS) {
            std::cerr << "Adc1881mDecoder: slot_id=" << slot_id
                      << " >= MAX_SLOTS=" << MAX_SLOTS << "\n";
            break;
        }

        SlotData &s = roc.slots[slot_id];
        s.present   = true;
        s.trigger   = 0;
        s.timestamp = 0;
        s.nchannels = 0;
        s.channel_mask = 0;

        // Parse data words for this board
        while (++idx < nwords && idx < word_end) {
            // Verify slot address matches
            if (((data[idx] >> 27) & 0x1F) != slot_id) {
                std::cerr << "Adc1881mDecoder: slot mismatch at word " << idx << "\n";
                continue;
            }

            uint32_t ch  = (data[idx] >> 17) & 0x3F;
            uint16_t val = data[idx] & 0x3FFF;  // 14-bit ADC value

            if (ch >= MAX_CHANNELS) continue;

            ChannelData &cd = s.channels[ch];
            cd.nsamples   = 1;
            cd.samples[0] = val;

            s.channel_mask |= (1ull << ch);
            s.nchannels++;
        }
        nslots++;
    }

    roc.nslots = nslots;
    return nslots;
}
