// prad2py.cpp — main module glue.
//
// The actual binding code lives in per-area translation units:
//   bind_dec.cpp   → prad2py.dec (evio reader, event data, TDC/SSP/VTP)
//   (future)       → prad2py.det (HyCal / GEM reconstruction)
//
// Each of those files defines a ``register_XXX(py::module_ &m)`` entry
// point that adds a submodule to the top-level module.
//
// Convenience helpers that span multiple areas (e.g. ``load_tdc_hits``)
// stay here at module root so user code can write
// ``prad2py.load_tdc_hits(...)`` without digging into submodules.

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include "EvChannel.h"
#include "DaqConfig.h"
#include "load_daq_config.h"
#include "TdcData.h"

#include <cstdlib>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

#ifndef DATABASE_DIR
#define DATABASE_DIR "."
#endif

namespace {

// Packed to match the numpy structured dtype below (itemsize == 17).
#pragma pack(push, 1)
struct NpHit {
    uint32_t event_num;
    uint32_t trigger_bits;
    uint16_t roc_tag;
    uint8_t  slot;
    uint8_t  channel;
    uint8_t  edge;
    uint32_t tdc;
};
#pragma pack(pop)
static_assert(sizeof(NpHit) == 17, "NpHit must be packed to 17 bytes");

std::string default_daq_config_path()
{
    const char *env = std::getenv("PRAD2_DATABASE_DIR");
    std::string dir = env ? env : DATABASE_DIR;
    return dir + "/daq_config.json";
}

py::dtype make_tdc_dtype()
{
    // Build an equivalent of:
    //   np.dtype([('event_num','<u4'), ('trigger_bits','<u4'), ('roc_tag','<u2'),
    //             ('slot','u1'), ('channel','u1'), ('edge','u1'), ('tdc','<u4')])
    //
    // Must go through py::dtype::from_args(...) — the plain dtype(list)
    // constructor inherited from object just wraps the list without
    // invoking NumPy's dtype() converter, and downstream use then fails
    // with "object of type 'list' is not an instance of dtype".
    py::list fields;
    fields.append(py::make_tuple("event_num",    "<u4"));
    fields.append(py::make_tuple("trigger_bits", "<u4"));
    fields.append(py::make_tuple("roc_tag",      "<u2"));
    fields.append(py::make_tuple("slot",         "u1"));
    fields.append(py::make_tuple("channel",      "u1"));
    fields.append(py::make_tuple("edge",         "u1"));
    fields.append(py::make_tuple("tdc",          "<u4"));
    return py::dtype::from_args(fields);
}

py::array load_tdc_hits(
    const std::string &path,
    const std::string &daq_config_in,
    int64_t max_events,
    int roc_filter)
{
    const std::string daq_config = daq_config_in.empty()
                                   ? default_daq_config_path()
                                   : daq_config_in;

    evc::DaqConfig cfg;
    if (!evc::load_daq_config(daq_config, cfg))
        throw std::runtime_error("Cannot load DAQ config: " + daq_config);

    evc::EvChannel ch;
    ch.SetConfig(cfg);
    if (ch.Open(path) != evc::status::success)
        throw std::runtime_error("Cannot open evio file: " + path);

    std::vector<NpHit> hits_buf;
    hits_buf.reserve(1u << 20);   // 1M hits preallocated

    auto event   = std::make_unique<fdec::EventData>();
    auto tdc_evt = std::make_unique<tdc::TdcEventData>();

    int64_t n_events = 0;

    {
        // Decoding is pure C++ — release the GIL so other Python threads
        // (e.g. the GUI event loop) can run while we chew through events.
        py::gil_scoped_release release;

        while (ch.Read() == evc::status::success) {
            if (!ch.Scan()) continue;
            if (ch.GetEventType() != evc::EventType::Physics) continue;

            const int nsub = ch.GetNEvents();
            for (int i = 0; i < nsub; ++i) {
                event->clear();
                tdc_evt->clear();
                ch.DecodeEvent(i, *event, nullptr, nullptr, tdc_evt.get());

                const uint32_t evnum = event->info.event_number;
                const uint32_t tbits = event->info.trigger_bits;

                for (int h = 0; h < tdc_evt->n_hits; ++h) {
                    const auto &hit = tdc_evt->hits[h];
                    if (roc_filter >= 0 &&
                        static_cast<int>(hit.roc_tag) != roc_filter)
                        continue;
                    NpHit p;
                    p.event_num    = evnum;
                    p.trigger_bits = tbits;
                    p.roc_tag      = static_cast<uint16_t>(hit.roc_tag);
                    p.slot         = hit.slot;
                    p.channel      = hit.channel;
                    p.edge         = hit.edge;
                    p.tdc          = hit.value;
                    hits_buf.push_back(p);
                }

                ++n_events;
                if (max_events > 0 && n_events >= max_events)
                    goto done;
            }
        }
    done:;
    }

    py::array arr(make_tdc_dtype(), hits_buf.size());
    if (!hits_buf.empty()) {
        std::memcpy(arr.mutable_data(), hits_buf.data(),
                    hits_buf.size() * sizeof(NpHit));
    }
    return arr;
}

} // anonymous namespace

// Defined in bind_dec.cpp — registers the ``prad2py.dec`` submodule.
void register_dec(py::module_ &m);

PYBIND11_MODULE(prad2py, m)
{
    m.doc() = "PRad-II (prad2dec + prad2det) Python bindings.";

    m.attr("__version__")    = "0.2.0";
    m.attr("DATABASE_DIR")   = DATABASE_DIR;

    m.def("default_daq_config", &default_daq_config_path,
          "Return the default daq_config.json path used by load_tdc_hits.");

    // Per-area submodules.  Phase 1: decoder.  Phase 2+: detector.
    register_dec(m);

    m.def(
        "load_tdc_hits",
        &load_tdc_hits,
        py::arg("path"),
        py::kw_only(),
        py::arg("daq_config") = std::string(""),
        py::arg("max_events") = int64_t(0),
        py::arg("roc_filter") = -1,
        R"doc(
Load V1190 TDC hits (bank 0xE107) from an EVIO file.

Parameters
----------
path : str
    Path to the .evio file.
daq_config : str, keyword-only
    Path to daq_config.json. Empty string uses the installed default
    (respects $PRAD2_DATABASE_DIR).
max_events : int, keyword-only
    Stop after N physics events. 0 (default) = read to end.
roc_filter : int, keyword-only
    Only keep hits whose parent ROC tag equals this value (e.g. 0x008E
    for the tagger crate). -1 (default) keeps all ROCs.

Returns
-------
numpy.ndarray
    Structured array, itemsize 17, with fields:
        event_num     <u4
        trigger_bits  <u4
        roc_tag       <u2
        slot          u1
        channel       u1
        edge          u1
        tdc           <u4
    One record per TDC hit.
)doc");
}
