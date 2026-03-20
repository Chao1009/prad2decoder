#pragma once
//=============================================================================
// Adc1881mDecoder.h — decode Fastbus ADC1881M raw data into RocData buffers
//
// PRad (original) used ADC1881M modules in Fastbus crates for HyCal readout.
// Data format: self-defined header (0xdc0adc00), board words with 14-bit ADC
// values, end marker (0xfabc0005).
//
// Outputs into the same RocData/SlotData/ChannelData structures as FADC250,
// storing the single ADC value as samples[0] with nsamples=1.
//=============================================================================

#include "Fadc250Data.h"
#include <cstdint>
#include <cstddef>

namespace fdec
{

class Adc1881mDecoder
{
public:
    // Header/footer markers used in CODA readout list
    static constexpr uint32_t DATA_BEGIN   = 0xdc0adc00;  // mask: 0xff0fff00
    static constexpr uint32_t DATA_BEGIN_MASK = 0xff0fff00;
    static constexpr uint32_t DATA_END     = 0xfabc0005;
    static constexpr uint32_t ALIGNMENT    = 0x00000000;

    // Decode one ROC's ADC1881M data bank into roc.
    // data points to the raw 32-bit words of the data bank payload.
    // nwords is the number of 32-bit words.
    // Returns number of slots decoded, or -1 on error.
    static int DecodeRoc(const uint32_t *data, size_t nwords, RocData &roc);
};

} // namespace fdec
