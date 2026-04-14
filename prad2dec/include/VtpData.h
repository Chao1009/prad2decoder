#pragma once
//=============================================================================
// VtpData.h — pre-allocated event data for VTP (0xE122) readout
//
// The VTP trigger processor ships self-describing 32-bit records. PRad-II
// cares about the ECAL records (EC_PEAK, EC_CLUSTER) — CLAS12 records are
// parsed past but not stored.
//=============================================================================

#include <cstdint>
#include <cstddef>

namespace vtp
{

// --- capacity limits --------------------------------------------------------
static constexpr int MAX_EC_PEAKS    = 512;  // per event, across all ROCs
static constexpr int MAX_EC_CLUSTERS = 64;
static constexpr int MAX_BLOCKS      = 16;   // one per VTP ROC per event

// --- record types -----------------------------------------------------------

// EC_PEAK (XML dict: 0x14)
//   w0: [26] inst, [25:24] view, [23:16] time
//   w1: [25:16] coord, [15:0] energy
struct EcPeak {
    uint32_t roc_tag;    // parent ROC bank tag
    uint8_t  inst;       // 0 or 1 (PCal vs HyCal instance)
    uint8_t  view;       // 0..3 (U/V/W strip view)
    uint8_t  time;       // 8-bit
    uint16_t coord;      // 10-bit strip/xtal coordinate
    uint16_t energy;     // 16-bit energy sum
};

// EC_CLUSTER (XML dict: 0x15)
//   w0: [26] inst, [23:16] time, [15:0] energy
//   w1: [29:20] coordW, [19:10] coordV, [9:0] coordU
struct EcCluster {
    uint32_t roc_tag;
    uint8_t  inst;
    uint8_t  time;
    uint16_t energy;
    uint16_t coordU;     // 10-bit
    uint16_t coordV;
    uint16_t coordW;
};

// VTP block (0x10 BLKHDR + 0x11 BLKTLR pair; 0x12 / 0x13 header data)
struct VtpBlock {
    uint32_t roc_tag;
    uint8_t  slot;            // BLKHDR [26:22]
    uint8_t  module_id;       // BLKHDR may carry module id in upper bits
    uint16_t block_number;    // BLKHDR [17:08]
    uint8_t  block_level;     // BLKHDR [7:0]
    uint32_t nwords;          // BLKTLR [21:0]
    uint32_t event_number;    // EVTHDR [26:0]
    uint64_t trigger_time;    // TRGTIME (48-bit)
    bool     has_trailer;     // set when BLKTLR was seen
    bool     trailer_mismatch; // trailer slot != header slot
};

// --- full event data --------------------------------------------------------
struct VtpEventData {
    int n_peaks    = 0;
    int n_clusters = 0;
    int n_blocks   = 0;

    EcPeak    peaks[MAX_EC_PEAKS];
    EcCluster clusters[MAX_EC_CLUSTERS];
    VtpBlock  blocks[MAX_BLOCKS];

    void clear()
    {
        n_peaks = 0;
        n_clusters = 0;
        n_blocks = 0;
    }
};

} // namespace vtp
