// =========================================================================
// data_source.cpp — Factory function for creating data sources
// =========================================================================

#include "data_source.h"
#include "evio_data_source.h"
#include "DaqConfig.h"

#ifdef WITH_ROOT
#include "root_data_source.h"
#endif

#include <algorithm>

std::unique_ptr<DataSource> createDataSource(
    const std::string &path,
    const evc::DaqConfig &daq_cfg,
    const std::unordered_map<int, uint32_t> &crate_to_roc)
{
    // detect file type by extension
    std::string lower = path;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);

    if (lower.find(".root") != std::string::npos) {
#ifdef WITH_ROOT
        return createRootDataSource(path, crate_to_roc);
#else
        return nullptr;  // ROOT support not compiled
#endif
    }

    // default: EVIO (.evio, .evio.0, .evio.00000, etc.)
    return std::make_unique<EvioDataSource>(daq_cfg);
}
