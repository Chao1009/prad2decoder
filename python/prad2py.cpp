// prad2py.cpp — main module glue.
//
// The actual binding code lives in per-area translation units:
//   bind_dec.cpp   → prad2py.dec (evio reader, event data, TDC/SSP/VTP)
//   bind_det.cpp   → prad2py.det (detector systems + reconstruction;
//                                   Phase 2a: GEM; 2b HyCal; 2c helpers)
//
// Each of those files defines a ``register_XXX(py::module_ &m)`` entry
// point that adds a submodule to the top-level module.
//
// No "do everything" helpers at module root — analyses should drive the
// per-event loop themselves via ``dec.EvChannel.select_event()`` plus
// ``info()``/``fadc()``/``gem()``/``tdc()``/``vtp()`` and accumulate into
// numpy / Python on their own terms.

#include <pybind11/pybind11.h>

#include "InstallPaths.h"

#include <cstdlib>
#include <string>

namespace py = pybind11;

#ifndef DATABASE_DIR
#define DATABASE_DIR "."
#endif

namespace {

std::string resolved_database_dir()
{
    // From <prefix>/lib/prad2py/prad2py.so the database is at
    // <prefix>/share/prad2evviewer/database.  Also accept the
    // "from <prefix>/bin/" layout so this code is usable if anyone ever
    // wires prad2py as a system-site-packages module instead.
    return prad2::resolve_data_dir(
        "PRAD2_DATABASE_DIR",
        {"../../share/prad2evviewer/database",
         "../share/prad2evviewer/database"},
        DATABASE_DIR);
}

std::string default_daq_config_path()
{
    return resolved_database_dir() + "/daq_config.json";
}

} // anonymous namespace

// Defined in bind_dec.cpp / bind_det.cpp — register the submodules.
void register_dec(py::module_ &m);
void register_det(py::module_ &m);

PYBIND11_MODULE(prad2py, m)
{
    m.doc() = "PRad-II (prad2dec + prad2det) Python bindings.";

    m.attr("__version__")    = "0.4.0";

    // Resolved at module-load time: env → module-relative → compile-time.
    // Exposes the same path `default_daq_config()` will use so analyses
    // can point their own lookups at the right place.
    m.attr("DATABASE_DIR")   = resolved_database_dir();

    m.def("default_daq_config", &default_daq_config_path,
          "Return the default daq_config.json path used by analyses.");

    // Per-area submodules.
    register_dec(m);    // prad2py.dec
    register_det(m);    // prad2py.det
}
