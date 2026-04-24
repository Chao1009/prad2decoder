#pragma once
//=============================================================================
// ConfigSetup.h — detector geometry configuration for PRad2
//
// Loads beam energy and HyCal/GEM coordinates from a JSON config file,
// and provides helpers to transform hit coordinates from the detector
// frame to the target/beam-centered frame. Also parses run numbers from
// input file names.
// Depends on PhysicsTools (HCHit/GEMHit/MollerData) and nlohmann::json.
//=============================================================================

#include "PhysicsTools.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace analysis {

namespace fs = std::filesystem;
// --- detector geometry configuration struct ---------------------------------
// Holds all run-specific detector geometry and beam parameters.
// Using a struct allows multi-run processing without shared mutable state:
//
//   auto geo1 = LoadCalibConfig(path, run1);
//   auto geo2 = LoadCalibConfig(path, run2);
//   TransformDetData(hits1, geo1);
//   TransformDetData(hits2, geo2);
//
struct CalibConfig {
    std::string energy_calib_file;
    float default_adc2mev  = 0.078f;
    float Ebeam      = 0.f;
    float target_x   = 0.f;
    float target_y   = 0.f;
    float target_z   = 0.f;
    float hycal_x    = 0.f;
    float hycal_y    = 0.f;
    float hycal_z    = 6225.0f;
    float hycal_tilt_x = 0.f;
    float hycal_tilt_y = 0.f;
    float hycal_tilt_z = 0.f;
    float gem_x[4]      = {-252.8f,  252.8f, -252.8f,  252.8f};
    float gem_y[4]      = {0.f, 0.f, 0.f, 0.f};
    float gem_z[4]      = {5423.0f, 5384.0f, 5823.0f, 5784.0f};
    float gem_tilt_x[4] = {0.f, 0.f, 0.f, 0.f};
    float gem_tilt_y[4] = {0.f, 0.f, 0.f, 0.f};
    float gem_tilt_z[4] = {0.f, 180.f, 0.f, 180.f};
};

// Global geometry config for single-run tools.
// Multi-run code should capture LoadCalibConfig()'s return value into a
// local CalibConfig instead of relying on this global.
inline CalibConfig gCalibConfig;

// Backward-compatible aliases pointing into gCalibConfig (zero overhead).
// Existing code that reads/writes Ebeam_, hycal_x_, gem_z_[] etc. continues
// to work without any modification.
inline std::string  &energy_calib_file_ = gCalibConfig.energy_calib_file;
inline float        &Ebeam_   = gCalibConfig.Ebeam;
inline float        &hycal_z_ = gCalibConfig.hycal_z;
inline float        &hycal_x_ = gCalibConfig.hycal_x;
inline float        &hycal_y_ = gCalibConfig.hycal_y;
inline float *const  gem_z_   = gCalibConfig.gem_z;
inline float *const  gem_x_   = gCalibConfig.gem_x;
inline float *const  gem_y_   = gCalibConfig.gem_y;

// --- config file loading ----------------------------------------------------
// Returns a CalibConfig populated from the best-matching entry in the JSON file.
// Selects the entry whose run_number is the largest value <= run_num.
// If run_num < 0 (unknown), uses the entry with the largest run_number.
//
// Single-run tools:  gCalibConfig = LoadCalibConfig(path, run);
// Multi-run tools:   auto geo1 = LoadCalibConfig(path, run1);
//                    auto geo2 = LoadCalibConfig(path, run2);
inline CalibConfig LoadCalibConfig(const std::string &transform_config, int run_num)
{
    CalibConfig result;   // start from defaults defined in CalibConfig

    std::ifstream cfg_f(transform_config);
    if (!cfg_f) {
        std::cerr << "Warning: cannot open config file " << transform_config << ", using defaults.\n";
        std::cerr << "Warning: Ebeam not set, may cause something wrong\n";
        return result;
    }
    auto cfg = nlohmann::json::parse(cfg_f, nullptr, false, true);
    if (cfg.is_discarded()) {
        std::cerr << "Warning: failed to parse " << transform_config << ", using defaults.\n";
        std::cerr << "Warning: Ebeam not set, may cause something wrong\n";
        return result;
    }
    if (!cfg.contains("configurations") || !cfg["configurations"].is_array()) {
        std::cerr << "Warning: " << transform_config << " has no \"configurations\" array, using defaults.\n";
        return result;
    }

    // find the best-matching entry
    const nlohmann::json *best = nullptr;
    int best_run = -1;

    if (run_num < 0) {
        std::cerr << "Warning: unknown run number, using the entry with the largest run_number.\n";
    }
    for (const auto &entry : cfg["configurations"]) {
        if (!entry.contains("run_number")) continue;
        int rn = entry["run_number"].get<int>();
        if (run_num < 0) {
            if (rn > best_run) { best = &entry; best_run = rn; }
        } else {
            if (rn <= run_num && rn > best_run) { best = &entry; best_run = rn; }
        }
    }

    if (best == nullptr) {
        std::cerr << "Warning: no matching configuration found in " << transform_config
                  << " for run " << run_num << ", using defaults.\n";
        return result;
    }

    const auto &c = *best;
    if (c.contains("beam_energy")) result.Ebeam = c["beam_energy"].get<float>();
    if (c.contains("calibration")) {
        const auto &cal = c["calibration"];
        if (cal.contains("file"))           result.energy_calib_file = cal["file"].get<std::string>();
        if (cal.contains("default_adc2mev")) result.default_adc2mev  = cal["default_adc2mev"].get<float>();
    }
    if (c.contains("target") && c["target"].is_array() && c["target"].size() >= 3) {
        result.target_x = c["target"][0].get<float>();
        result.target_y = c["target"][1].get<float>();
        result.target_z = c["target"][2].get<float>();
    }
    if (c.contains("hycal")) {
        const auto &h = c["hycal"];
        if (h.contains("position") && h["position"].is_array() && h["position"].size() >= 3) {
            result.hycal_x = h["position"][0].get<float>();
            result.hycal_y = h["position"][1].get<float>();
            result.hycal_z = h["position"][2].get<float>();
        }
        if (h.contains("tilting") && h["tilting"].is_array() && h["tilting"].size() >= 3) {
            result.hycal_tilt_x = h["tilting"][0].get<float>();
            result.hycal_tilt_y = h["tilting"][1].get<float>();
            result.hycal_tilt_z = h["tilting"][2].get<float>();
        }
    }
    if (c.contains("gem") && c["gem"].is_array()) {
        for (const auto &g : c["gem"]) {
            if (!g.contains("id")) continue;
            int id = g["id"].get<int>();
            if (id < 0 || id >= 4) continue;
            if (g.contains("position") && g["position"].is_array() && g["position"].size() >= 3) {
                result.gem_x[id] = g["position"][0].get<float>();
                result.gem_y[id] = g["position"][1].get<float>();
                result.gem_z[id] = g["position"][2].get<float>();
            }
            if (g.contains("tilting") && g["tilting"].is_array() && g["tilting"].size() >= 3) {
                result.gem_tilt_x[id] = g["tilting"][0].get<float>();
                result.gem_tilt_y[id] = g["tilting"][1].get<float>();
                result.gem_tilt_z[id] = g["tilting"][2].get<float>();
            }
        }
    }
    std::cerr << "Loaded detector coordinates config (run_number=" << best_run
              << ") from: " << transform_config << "\n";
    return result;
}

// --- config file writing ----------------------------------------------------
// Appends a new entry (run_number + CalibConfig) to the "configurations" array
// in the given JSON file. If the file does not exist, it is created from
// scratch. If an entry with the same run_number already exists, it is
// overwritten in-place. The updated JSON is written back atomically via a
// temporary file to avoid corruption on failure.
inline bool WriteTransformConfig(const std::string &transform_config, int run_num,
                                 const CalibConfig &geo)
{
    // --- load existing file (or start empty) --------------------------------
    nlohmann::json cfg;
    {
        std::ifstream cfg_f(transform_config);
        if (cfg_f) {
            cfg = nlohmann::json::parse(cfg_f, nullptr, false, true);
            if (cfg.is_discarded()) {
                std::cerr << "Warning: failed to parse " << transform_config
                          << ", will overwrite with new data.\n";
                cfg = nlohmann::json::object();
            }
        } else {
            cfg = nlohmann::json::object();
        }
    }

    // ensure top-level structure
    if (!cfg.contains("configurations") || !cfg["configurations"].is_array())
        cfg["configurations"] = nlohmann::json::array();

    // --- build new entry ----------------------------------------------------
    nlohmann::json entry;
    entry["run_number"]  = run_num;
    entry["beam_energy"] = geo.Ebeam;
    entry["calibration"]["file"]            = geo.energy_calib_file;
    entry["calibration"]["default_adc2mev"] = geo.default_adc2mev;
    entry["target"] = nlohmann::json::array({geo.target_x, geo.target_y, geo.target_z});
    entry["hycal"]["position"] = nlohmann::json::array({geo.hycal_x, geo.hycal_y, geo.hycal_z});
    entry["hycal"]["tilting"]  = nlohmann::json::array({geo.hycal_tilt_x, geo.hycal_tilt_y, geo.hycal_tilt_z});
    entry["gem"] = nlohmann::json::array();
    for (int i = 0; i < 4; ++i) {
        nlohmann::json g;
        g["id"]       = i;
        g["position"] = nlohmann::json::array({geo.gem_x[i], geo.gem_y[i], geo.gem_z[i]});
        g["tilting"]  = nlohmann::json::array({geo.gem_tilt_x[i], geo.gem_tilt_y[i], geo.gem_tilt_z[i]});
        entry["gem"].push_back(g);
    }

    // --- replace existing entry or append -----------------------------------
    auto &arr = cfg["configurations"];
    bool replaced = false;
    for (auto &e : arr) {
        if (e.contains("run_number") && e["run_number"].get<int>() == run_num) {
            e = entry;
            replaced = true;
            break;
        }
    }
    if (!replaced) arr.push_back(entry);

    // sort by run_number for readability
    std::sort(arr.begin(), arr.end(), [](const nlohmann::json &a, const nlohmann::json &b) {
        int ra = a.contains("run_number") ? a["run_number"].get<int>() : -1;
        int rb = b.contains("run_number") ? b["run_number"].get<int>() : -1;
        return ra < rb;
    });

    // --- write back atomically via a temporary file -------------------------
    std::string tmp_path = transform_config + ".tmp";
    {
        std::ofstream out(tmp_path);
        if (!out) {
            std::cerr << "Error: cannot write to " << tmp_path << "\n";
            return false;
        }
        out << cfg.dump(4) << "\n";
    }
    if (std::rename(tmp_path.c_str(), transform_config.c_str()) != 0) {
        std::cerr << "Error: failed to rename " << tmp_path << " -> " << transform_config << "\n";
        return false;
    }
    std::cerr << (replaced ? "Updated" : "Appended") << " run_number=" << run_num
              << " in " << transform_config << "\n";
    return true;
}


// Transform detector-frame coordinates to the target/beam-centered frame.
//
// Explicit-offset overloads (float beamX, float beamY, float ZfromTarget):
//   Always available; callers supply the numbers directly.
//
// CalibConfig overloads (const CalibConfig &geo = gCalibConfig):
//   Use the geometry loaded by LoadCalibConfig().
//   Default argument = gCalibConfig, so existing single-arg calls like
//     TransformDetData(hc_hits);
//   still compile and use the global config unchanged.
//   Multi-run code can pass an explicit CalibConfig:
//     TransformDetData(hc_hits, geo1);

// -- single-hit primitives (used internally by the vector overloads) ---------
inline void TransformDetData(HCHit &h, float beamX, float beamY, float ZfromTarget)
{
    h.x -= beamX;
    h.y -= beamY;
    h.z += ZfromTarget;
}
inline void TransformDetData(GEMHit &h, float beamX, float beamY, float ZfromTarget)
{
    h.x -= beamX;
    h.y -= beamY;
    h.z += ZfromTarget;
}

// Apply successive rotations Rz → Ry → Rx (extrinsic, small-angle convention).
// Each angle is in degrees.  Only non-zero axes incur any computation cost.
inline void RotateDetData(HCHit &h, float x_deg, float y_deg, float z_deg)
{
    constexpr float kDeg2Rad = 3.14159265f / 180.f;
    float x = h.x, y = h.y, z = h.z;

    if (z_deg != 0.f) {
        float c = std::cos(z_deg * kDeg2Rad), s = std::sin(z_deg * kDeg2Rad);
        float nx = x * c - y * s;
        float ny = x * s + y * c;
        x = nx; y = ny;
    }
    if (y_deg != 0.f) {
        float c = std::cos(y_deg * kDeg2Rad), s = std::sin(y_deg * kDeg2Rad);
        float nx =  x * c + z * s;
        float nz = -x * s + z * c;
        x = nx; z = nz;
    }
    if (x_deg != 0.f) {
        float c = std::cos(x_deg * kDeg2Rad), s = std::sin(x_deg * kDeg2Rad);
        float ny = y * c - z * s;
        float nz = y * s + z * c;
        y = ny; z = nz;
    }
    h.x = x; h.y = y; h.z = z;
}

inline void RotateDetData(GEMHit &h, float x_deg, float y_deg, float z_deg)
{
    constexpr float kDeg2Rad = 3.14159265f / 180.f;
    float x = h.x, y = h.y, z = h.z;

    if (z_deg != 0.f) {
        float c = std::cos(z_deg * kDeg2Rad), s = std::sin(z_deg * kDeg2Rad);
        float nx = x * c - y * s;
        float ny = x * s + y * c;
        x = nx; y = ny;
    }
    if (y_deg != 0.f) {
        float c = std::cos(y_deg * kDeg2Rad), s = std::sin(y_deg * kDeg2Rad);
        float nx =  x * c + z * s;
        float nz = -x * s + z * c;
        x = nx; z = nz;
    }
    if (x_deg != 0.f) {
        float c = std::cos(x_deg * kDeg2Rad), s = std::sin(x_deg * kDeg2Rad);
        float ny = y * c - z * s;
        float nz = y * s + z * c;
        y = ny; z = nz;
    }
    h.x = x; h.y = y; h.z = z;
}

// -- HCHit vector ------------------------------------------------------------
inline void RotateDetData(std::vector<HCHit> &hc_hits,
                          float x_deg, float y_deg, float z_deg)
{
    for (auto &h : hc_hits) RotateDetData(h, x_deg, y_deg, z_deg);
}

// -- GEMHit vector -----------------------------------------------------------
inline void RotateDetData(std::vector<GEMHit> &gem_hits,
                          float x_deg, float y_deg, float z_deg)
{
    for (auto &h : gem_hits) RotateDetData(h, x_deg, y_deg, z_deg);
}

// -- CalibConfig overloads (use tilting angles stored in config) -------------
inline void RotateDetData(std::vector<HCHit> &hc_hits,
                          const CalibConfig &geo = gCalibConfig)
{
    RotateDetData(hc_hits, geo.hycal_tilt_x, geo.hycal_tilt_y, geo.hycal_tilt_z);
}

inline void RotateDetData(std::vector<GEMHit> &gem_hits,
                          const CalibConfig &geo = gCalibConfig)
{
    for (auto &h : gem_hits) {
        int det_id = h.det_id;
        if (det_id >= 0 && det_id < 4) {
            RotateDetData(h, geo.gem_tilt_x[det_id],
                             geo.gem_tilt_y[det_id],
                             geo.gem_tilt_z[det_id]);
        }
    }
}

// -- HCHit vector ------------------------------------------------------------
inline void TransformDetData(std::vector<HCHit> &hc_hits, float beamX, float beamY, float ZfromTarget)
{
    for (auto &h : hc_hits) TransformDetData(h, beamX, beamY, ZfromTarget);
}

inline void TransformDetData(std::vector<HCHit> &hc_hits, const CalibConfig &geo = gCalibConfig)
{
    TransformDetData(hc_hits, geo.hycal_x, geo.hycal_y, geo.hycal_z);
}

// -- GEMHit vector -----------------------------------------------------------
inline void TransformDetData(std::vector<GEMHit> &gem_hits, float beamX, float beamY, float ZfromTarget)
{
    for (auto &h : gem_hits) TransformDetData(h, beamX, beamY, ZfromTarget);
}

// Each GEM hit is transformed using its own detector id.
inline void TransformDetData(std::vector<GEMHit> &gem_hits, const CalibConfig &geo = gCalibConfig)
{
    for (auto &h : gem_hits) {
        int det_id = h.det_id;
        if (det_id >= 0 && det_id < 4) {
            TransformDetData(h, geo.gem_x[det_id], geo.gem_y[det_id], geo.gem_z[det_id]);
        } else {
            std::cerr << "Warning: Invalid GEM det_id " << det_id << " for coordinate transformation\n";
        }
    }
}

// -- MollerData --------------------------------------------------------------
inline void TransformDetData(MollerData &mollers, float beamX, float beamY, float ZfromTarget)
{
    for (auto &moller : mollers) {
        moller.first.x  -= beamX;
        moller.first.y  -= beamY;
        moller.first.z  += ZfromTarget;
        moller.second.x -= beamX;
        moller.second.y -= beamY;
        moller.second.z += ZfromTarget;
    }
}

// --- run number utilities ---------------------------------------------------
// Extract the run number embedded in a file name of the form
// ".../prad_<digits>...". Returns "unknown" / -1 on failure.
inline std::string get_run_str(const std::string &file_name)
{
    std::string fname = fs::path(file_name).filename().string();
    auto ppos = fname.find("prad_");
    if (ppos != std::string::npos) {
        size_t s = ppos + 5;
        size_t e = s;
        while (e < fname.size() && std::isdigit((unsigned char)fname[e])) e++;
        if (e > s) return std::to_string(std::stoul(fname.substr(s, e - s)));
    }
    std::cerr << "Warning: cannot extract run number from file name " << file_name << ", using 'unknown'.\n";
    return "unknown";
}

inline int get_run_int(const std::string &file_name)
{
    std::string run_str = get_run_str(file_name);
    if (run_str == "unknown") return -1;
    return std::stoi(run_str);
}

} // namespace analysis
