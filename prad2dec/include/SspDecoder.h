#pragma once
//=============================================================================
// SspDecoder.h — decode SSP/MPD raw data banks into pre-allocated buffers
//
// Decodes the SSP bitfield format used by MPD electronics for GEM readout.
// Each 32-bit word is self-describing via a data_type_tag field (bits 27-30).
//=============================================================================

#include "SspData.h"
#include <cstddef>

namespace ssp
{

class SspDecoder
{
public:
    // Decode one ROC's SSP raw data bank into evt.
    // crate_id: crate identifier (from parent ROC bank tag mapping).
    // Returns number of APVs decoded, or -1 on error.
    static int DecodeRoc(const uint32_t *data, size_t nwords,
                         int crate_id, SspEventData &evt);
};

} // namespace ssp
