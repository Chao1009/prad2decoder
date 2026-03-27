"""
Shared GEM strip mapping implementation.

Matches GemSystem::buildStripMap in prad2ana. Configurable per-APV via
the same parameters as gem_map.json.
"""


def map_strip(ch, plane_index, orient, pin_rotate=0, shared_pos=-1,
              hybrid_board=True, apv_channels=128, readout_center=32):
    """Map APV channel to plane-wide strip number.

    Returns (local_strip, plane_strip).
    """
    N = apv_channels
    readout_off = readout_center + pin_rotate
    eff_pos = shared_pos if shared_pos >= 0 else plane_index
    plane_shift = (eff_pos - plane_index) * N - pin_rotate

    # Step 1: APV25 internal channel mapping
    strip = 32 * (ch % 4) + 8 * (ch // 4) - 31 * (ch // 16)

    # Step 2: hybrid board pin conversion
    if hybrid_board:
        strip = strip + 1 + strip % 4 - 5 * ((strip // 4) % 2)

    # Step 3: readout strip mapping
    if readout_off > 0:
        if strip & 1:
            strip = readout_off - (strip + 1) // 2
        else:
            strip = readout_off + strip // 2

    # Step 4: channel mask
    strip &= (N - 1)
    local = strip

    # Step 5: orient flip
    if orient == 1:
        strip = (N - 1) - strip

    # Step 6: plane-wide strip number
    strip += plane_shift + plane_index * N

    return local, strip


def map_apv_strips(apv, apv_channels=128, readout_center=32):
    """Map all channels of an APV entry (from gem_map.json) to plane strip numbers.

    Returns list of plane_strip for ch 0..apv_channels-1.
    """
    pos = apv["pos"]
    orient = apv["orient"]
    pin_rotate = apv.get("pin_rotate", 0)
    shared_pos = apv.get("shared_pos", -1)
    hybrid_board = apv.get("hybrid_board", True)

    return [map_strip(ch, pos, orient,
                      pin_rotate=pin_rotate,
                      shared_pos=shared_pos,
                      hybrid_board=hybrid_board,
                      apv_channels=apv_channels,
                      readout_center=readout_center)[1]
            for ch in range(apv_channels)]
