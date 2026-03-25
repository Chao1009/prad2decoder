#include "SspDecoder.h"
#include <cstdio>

using namespace ssp;

//=============================================================================
// SSP bitfield structures (from sspApvdec.h)
//
// Each 32-bit SSP data word has:
//   bit 31:    data_type_defining (1 = new type, 0 = continuation)
//   bits 27-30: data_type_tag (when defining)
//   bits 0-26:  payload (type-specific)
//=============================================================================

namespace {

// Generic word — extract type info
struct GenericWord {
    uint32_t payload        : 27;
    uint32_t data_type_tag  : 4;
    uint32_t data_type_defining : 1;
};

// Type 0: Block Header
struct BlockHeader {
    uint32_t number_of_events : 8;
    uint32_t event_block_number : 10;
    uint32_t module_ID       : 4;
    uint32_t slot_number     : 5;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

// Type 1: Block Trailer
struct BlockTrailer {
    uint32_t words_in_block  : 22;
    uint32_t slot_number     : 5;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

// Type 2: Event Header
struct EventHeader {
    uint32_t trigger_number  : 27;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

// Type 3: Trigger Time (word 1)
struct TriggerTime1 {
    uint32_t trigger_time_l  : 24;
    uint32_t undef           : 3;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

// Type 3: Trigger Time (word 2)
struct TriggerTime2 {
    uint32_t trigger_time_h  : 24;
    uint32_t undef           : 3;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

// Type 5 (defining): MPD Frame Header
struct MpdFrame {
    uint32_t mpd_id          : 5;
    uint32_t undef           : 11;
    uint32_t fiber           : 6;
    uint32_t flags           : 5;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

// Type 5 (non-defining, word 1): APV Data — samples 0,1 + channel[4:0]
struct ApvData1 {
    uint32_t apv_sample0     : 13;
    uint32_t apv_sample1     : 13;
    uint32_t apv_channel_40  : 5;
    uint32_t data_type_defining : 1;
};

// Type 5 (non-defining, word 2): APV Data — samples 2,3 + channel[6:5]
struct ApvData2 {
    uint32_t apv_sample2     : 13;
    uint32_t apv_sample3     : 13;
    uint32_t apv_channel_65  : 5;
    uint32_t data_type_defining : 1;
};

// Type 5 (non-defining, word 3): APV Data — samples 4,5 + apv_id
struct ApvData3 {
    uint32_t apv_sample4     : 13;
    uint32_t apv_sample5     : 13;
    uint32_t apv_id          : 5;
    uint32_t data_type_defining : 1;
};

// MPD Timestamp Header (types 4, 6-12)
struct MpdTimestamp1 {
    uint32_t timestamp_fine     : 8;
    uint32_t timestamp_coarse0  : 16;
    uint32_t undef              : 3;
    uint32_t data_type_tag      : 4;
    uint32_t data_type_defining : 1;
};

struct MpdTimestamp2 {
    uint32_t timestamp_coarse1  : 24;
    uint32_t undef              : 7;
    uint32_t data_type_defining : 1;
};

struct MpdTimestamp3 {
    uint32_t event_count        : 20;
    uint32_t undef              : 11;
    uint32_t data_type_defining : 1;
};

// Type 0xD: MPD Debug Header (online common mode)
struct DebugHeader1 {
    uint32_t CM_T0           : 13;
    uint32_t CM_T1           : 13;
    uint32_t undef           : 1;
    uint32_t data_type_tag   : 4;
    uint32_t data_type_defining : 1;
};

struct DebugHeader2 {
    uint32_t CM_T2           : 13;
    uint32_t CM_T3           : 13;
    uint32_t undef           : 5;
    uint32_t data_type_defining : 1;
};

struct DebugHeader3 {
    uint32_t CM_T4           : 13;
    uint32_t CM_T5           : 13;
    uint32_t undef           : 5;
    uint32_t data_type_defining : 1;
};

// Sign-extend 13-bit value to int16_t
inline int16_t signExtend13(uint32_t val)
{
    // Extract 13 bits, then sign-extend via arithmetic shift
    int32_t s = static_cast<int32_t>(val & 0x1FFF);
    if (s & 0x1000) s |= ~0x1FFF;  // sign bit set → extend
    return static_cast<int16_t>(s);
}

// Helper union for type-punning
union DataWord {
    uint32_t      raw;
    GenericWord   generic;
    BlockHeader   block_header;
    BlockTrailer  block_trailer;
    EventHeader   event_header;
    TriggerTime1  trig_time1;
    TriggerTime2  trig_time2;
    MpdFrame      mpd_frame;
    ApvData1      apv1;
    ApvData2      apv2;
    ApvData3      apv3;
    MpdTimestamp1  ts1;
    MpdTimestamp2  ts2;
    MpdTimestamp3  ts3;
    DebugHeader1  dbg1;
    DebugHeader2  dbg2;
    DebugHeader3  dbg3;
};

} // anonymous namespace

//=============================================================================
// SspDecoder::DecodeRoc — state machine decoding of SSP raw data
//
// Reentrant: all state is local (no static variables).
//=============================================================================

int SspDecoder::DecodeRoc(const uint32_t *data, size_t nwords,
                          int crate_id, SspEventData &evt)
{
    // --- decoder state (all local) ---
    int type_last = 15;       // initialize to FILLER type
    int time_last = 0;
    int apv_data_word = 0;
    int mpd_debug_word = 0;
    int mpd_timestamp_word = 0;

    // Current APV address being built
    int cur_mpd_id = -1;
    int cur_apv_id = -1;
    int cur_strip  = -1;
    uint32_t cur_flags = 0;

    // Temporary strip samples
    int16_t strip_samples[SSP_TIME_SAMPLES];

    int apvs_decoded = 0;

    for (size_t i = 0; i < nwords; ++i) {
        DataWord w;
        w.raw = data[i];

        int new_type = 0;
        int type_current;

        if (w.generic.data_type_defining) {
            new_type = 1;
            type_current = w.generic.data_type_tag;
        } else {
            new_type = 0;
            type_current = type_last;
        }

        switch (type_current) {

        case 0: { // BLOCK HEADER
            // slot_number identifies the SSP board
            // For VTP readout, crate_id comes from parent ROC tag
            break;
        }

        case 1: { // BLOCK TRAILER
            break;
        }

        case 2: { // EVENT HEADER
            break;
        }

        case 3: { // TRIGGER TIME
            if (new_type) {
                time_last = 1;
            } else {
                time_last = 0;
            }
            break;
        }

        case 5: { // MPD FRAME or APV DATA
            if (new_type) {
                // MPD Frame Header — new MPD/fiber
                cur_mpd_id = w.mpd_frame.fiber;
                cur_flags  = w.mpd_frame.flags;
                apv_data_word = 1;
            } else {
                // APV Data — 3 words per strip
                switch (apv_data_word) {
                case 1: {
                    cur_strip = w.apv1.apv_channel_40;
                    strip_samples[0] = signExtend13(w.apv1.apv_sample0);
                    strip_samples[1] = signExtend13(w.apv1.apv_sample1);
                    apv_data_word = 2;
                    break;
                }
                case 2: {
                    cur_strip |= (w.apv2.apv_channel_65 << 5);
                    strip_samples[2] = signExtend13(w.apv2.apv_sample2);
                    strip_samples[3] = signExtend13(w.apv2.apv_sample3);
                    apv_data_word = 3;
                    break;
                }
                case 3: {
                    cur_apv_id = w.apv3.apv_id;
                    strip_samples[4] = signExtend13(w.apv3.apv_sample4);
                    strip_samples[5] = signExtend13(w.apv3.apv_sample5);
                    apv_data_word = 1;

                    // Strip complete — store if valid (< 128)
                    if (cur_strip < APV_STRIP_SIZE && cur_mpd_id >= 0 && cur_apv_id >= 0) {
                        MpdData *mpd = evt.findOrCreateMpd(crate_id, cur_mpd_id);
                        if (mpd && cur_apv_id < MAX_APVS_PER_MPD) {
                            ApvData &apv = mpd->apvs[cur_apv_id];
                            if (!apv.present) {
                                apv.present = true;
                                apv.addr = {crate_id, cur_mpd_id, cur_apv_id};
                                apv.flags = cur_flags;
                                mpd->napvs++;
                                apvs_decoded++;
                            }
                            for (int ts = 0; ts < SSP_TIME_SAMPLES; ++ts)
                                apv.setStrip(cur_strip, ts, strip_samples[ts]);
                        }
                    }
                    break;
                }
                default:
                    break;
                }
            }
            break;
        }

        case 4:
        case 6: case 7: case 8: case 9:
        case 10: case 11: case 12: {
            // MPD TIMESTAMP HEADER
            if (new_type) {
                mpd_timestamp_word = 1;
            } else {
                switch (mpd_timestamp_word) {
                case 1: mpd_timestamp_word = 2; break;
                case 2: mpd_timestamp_word = 0; break;
                default: break;
                }
            }
            break;
        }

        case 0xD: { // MPD DEBUG HEADER (online common mode)
            if (new_type) {
                mpd_debug_word = 1;
                // CM_T0, CM_T1
                if (cur_mpd_id >= 0 && cur_apv_id >= 0) {
                    MpdData *mpd = evt.findOrCreateMpd(crate_id, cur_mpd_id);
                    if (mpd && cur_apv_id < MAX_APVS_PER_MPD) {
                        ApvData &apv = mpd->apvs[cur_apv_id];
                        apv.online_cm[0] = static_cast<int16_t>(w.dbg1.CM_T0);
                        apv.online_cm[1] = static_cast<int16_t>(w.dbg1.CM_T1);
                        apv.has_online_cm = true;
                    }
                }
            } else {
                switch (mpd_debug_word) {
                case 1: {
                    mpd_debug_word = 2;
                    if (cur_mpd_id >= 0 && cur_apv_id >= 0) {
                        MpdData *mpd = evt.findOrCreateMpd(crate_id, cur_mpd_id);
                        if (mpd && cur_apv_id < MAX_APVS_PER_MPD) {
                            ApvData &apv = mpd->apvs[cur_apv_id];
                            apv.online_cm[2] = static_cast<int16_t>(w.dbg2.CM_T2);
                            apv.online_cm[3] = static_cast<int16_t>(w.dbg2.CM_T3);
                        }
                    }
                    break;
                }
                case 2: {
                    mpd_debug_word = 0;
                    // After debug header, reset to APV data type for following APVs
                    type_current = 5;
                    if (cur_mpd_id >= 0 && cur_apv_id >= 0) {
                        MpdData *mpd = evt.findOrCreateMpd(crate_id, cur_mpd_id);
                        if (mpd && cur_apv_id < MAX_APVS_PER_MPD) {
                            ApvData &apv = mpd->apvs[cur_apv_id];
                            apv.online_cm[4] = static_cast<int16_t>(w.dbg3.CM_T4);
                            apv.online_cm[5] = static_cast<int16_t>(w.dbg3.CM_T5);
                        }
                    }
                    break;
                }
                default:
                    break;
                }
            }
            break;
        }

        case 0xE: // DATA NOT VALID (type tag 14 = 0xE in 4-bit field)
        case 0xF: // FILLER WORD (type tag 15 = 0xF in 4-bit field)
            break;

        default:
            break;
        }

        type_last = type_current;
    }

    return apvs_decoded;
}
