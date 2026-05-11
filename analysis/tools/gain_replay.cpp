// gain_replay.cpp — standalone gain monitoring / LMS calibration tool.
//
// Reproduces the gain-factor computation from Replay::Process(--gain_plot)
// as a standalone tool that reads raw EVIO files without writing a full
// replay ROOT output.
//
// Event classification (identical to Replay::Process):
//   LMS event   : trigger_bit 24 set  AND  nch > 1000
//                 → fill mod_lms[W_id-1]  (PbWO4, 1 peak, peak_integral)
//                 → fill ref_lms[lms-1]   (LMS channels, 1 peak)
//   Alpha event : trigger_bit 25 set  AND  nch < 50
//                 → fill ref_alpha[lms-1] (LMS channels, 1 peak)
//
// After the loop (same formulas as Replay::Process):
//   refPMT_ratio[j] = ref_lms[j].peak / ref_alpha[j].peak
//   gain_W[i][j]    = mod_lms[i].peak / refPMT_ratio[j]
//
// Output:
//   prad_XXXXXX_LMS.dat   (7-column format consumed by LoadGainFactors())
//   prad_XXXXXX_LMS.root  (histograms for QA, optional)
//
// Usage: gain_replay <evio_file_or_dir> [...]
//        [-o output.dat] [-R output.root]
//        [-D daq_config.json] [-d hycal_map.json]
//        [-n max_events] [-r ref_run]

#include "Replay.h"
#include "gain_factor.h"
#include "ConfigSetup.h"
#include "InstallPaths.h"
#include "load_daq_config.h"
#include "PulseTemplateStore.h"

#include <TFile.h>
#include <TH1F.h>
#include <TF1.h>

#include <nlohmann/json.hpp>
#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <getopt.h>
#include <iomanip>
#include <iostream>
#include <string>
#include <unordered_map>
#include <vector>

#ifndef DATABASE_DIR
#define DATABASE_DIR "."
#endif

static std::vector<std::string> collectEvioFiles(const std::string &path)
{
    std::vector<std::string> files;
    if (std::filesystem::is_directory(path)) {
        for (auto &entry : std::filesystem::directory_iterator(path)) {
            if (entry.is_regular_file() &&
                entry.path().filename().string().find(".evio") != std::string::npos)
                files.push_back(entry.path().string());
        }
        std::sort(files.begin(), files.end());
    } else {
        files.push_back(path);
    }
    return files;
}

int main(int argc, char *argv[])
{
    std::string output_dat, output_root, daq_config, hycal_map_file;
    int max_events = -1;
    int ref_run    = -1;

    std::string db_dir = prad2::resolve_data_dir(
        "PRAD2_DATABASE_DIR",
        {"../share/prad2evviewer/database"},
        DATABASE_DIR);
    daq_config = db_dir + "/daq_config.json";

    static struct option long_opts[] = {
        {"ref_run", required_argument, nullptr, 'r'},
        {nullptr,   0,                 nullptr,  0 }
    };
    int opt;
    while ((opt = getopt_long(argc, argv, "o:R:n:D:d:r:", long_opts, nullptr)) != -1) {
        switch (opt) {
            case 'o': output_dat     = optarg;           break;
            case 'R': output_root    = optarg;           break;
            case 'n': max_events     = std::atoi(optarg); break;
            case 'D': daq_config     = optarg;           break;
            case 'd': hycal_map_file = optarg;           break;
            case 'r': ref_run        = std::atoi(optarg); break;
            default:
                std::cerr << "Usage: gain_replay <evio_file_or_dir> [...]\n"
                          << "       [-o output.dat] [-R output.root]\n"
                          << "       [-D daq_config.json] [-d hycal_map.json]\n"
                          << "       [-n max_events] [--ref_run|-r ref_run]\n";
                return 1;
        }
    }

    std::vector<std::string> evio_files;
    for (int i = optind; i < argc; ++i) {
        auto f = collectEvioFiles(argv[i]);
        evio_files.insert(evio_files.end(), f.begin(), f.end());
    }
    if (evio_files.empty()) {
        std::cerr << "Usage: gain_replay <evio_file_or_dir> [...]\n"
                  << "       [-o output.dat] [-R output.root]\n"
                  << "       [-D daq_config.json] [-d hycal_map.json]\n"
                  << "       [-n max_events] [--ref_run|-r ref_run]\n";
        return 1;
    }

    // infer run number from first file; build default output names
    int run_num = analysis::get_run_int(evio_files[0]);
    if (output_dat.empty())  output_dat  = Form("prad_%06d_LMS.dat",  run_num);
    if (output_root.empty()) output_root = Form("prad_%06d_LMS.root", run_num);

    // ── channel mapping ──────────────────────────────────────────────────
    analysis::Replay replay;
    if (!daq_config.empty()) replay.LoadDaqConfig(daq_config);
    if (hycal_map_file.empty()) hycal_map_file = db_dir + "/hycal_map.json";
    replay.LoadHyCalMap(hycal_map_file);
    std::cerr << "Using HyCal map: " << hycal_map_file << "\n";

    // load RunConfig for gain reference
    auto gRunConfig = analysis::LoadRunConfig(
        db_dir + "/runinfo/2p1_general.json", run_num);
    if (ref_run >= 0) gRunConfig.gain_ref_run = ref_run;

    // ── separate DaqConfig for WaveAnalyzer / EvChannel ─────────────────
    evc::DaqConfig daq_cfg;
    evc::load_daq_config(daq_config, daq_cfg);

    // ROC tag → crate index mapping
    std::unordered_map<int, int> roc_to_crate;
    {
        std::ifstream dcf(daq_config);
        if (dcf.is_open()) {
            auto dcj = nlohmann::json::parse(dcf, nullptr, false, true);
            if (dcj.contains("roc_tags") && dcj["roc_tags"].is_array()) {
                for (auto &entry : dcj["roc_tags"]) {
                    int tag   = std::stoi(entry.at("tag").get<std::string>(), nullptr, 16);
                    int crate = entry.at("crate").get<int>();
                    roc_to_crate[tag] = crate;
                }
            }
        }
    }

    // ── histograms (same binning as Replay::Process) ─────────────────────
    TH1F *ref_lms[3], *ref_alpha[3], *mod_lms[1156];
    for (int i = 0; i < 3; ++i) {
        ref_lms[i]   = new TH1F(Form("ref_lms%d",   i+1), Form("Reference LMS%d",   i+1), 300, 0, 15000);
        ref_alpha[i] = new TH1F(Form("ref_alpha%d", i+1), Form("Reference alpha%d", i+1), 300, 0, 15000);
    }
    for (int i = 0; i < 1156; ++i)
        mod_lms[i] = new TH1F(Form("mod_lms_W%d", i+1), Form("W%d LMS", i+1), 300, 0, 15000);

    // ── event loop ────────────────────────────────────────────────────────
    evc::EvChannel ch;
    ch.SetConfig(daq_cfg);

    fdec::WaveAnalyzer ana(daq_cfg.wave_cfg);
    fdec::PulseTemplateStore template_store;
    if (daq_cfg.wave_cfg.nnls_deconv.enabled &&
        !daq_cfg.wave_cfg.nnls_deconv.template_file.empty()) {
        template_store.LoadFromFile(
            db_dir + "/" + daq_cfg.wave_cfg.nnls_deconv.template_file,
            daq_cfg.wave_cfg);
    }
    ana.SetTemplateStore(&template_store);
    fdec::WaveResult wres;

    auto event = std::make_unique<fdec::EventData>();
    int total = 0;
    bool done = false;

    for (const auto &input_evio : evio_files) {
        if (done) break;
        if (ch.OpenAuto(input_evio) != evc::status::success) {
            std::cerr << "Cannot open " << input_evio << " — skipping\n";
            continue;
        }
        std::cerr << "[file] " << input_evio << "\n";

        while (!done && ch.Read() == evc::status::success) {
            if (!ch.Scan()) continue;
            if (ch.GetEventType() != evc::EventType::Physics) continue;

            for (int ie = 0; ie < ch.GetNEvents() && !done; ++ie) {
                event->clear();
                if (!ch.DecodeEvent(ie, *event)) continue;
                if (max_events > 0 && total >= max_events) { done = true; break; }

                // ── first pass: decode all FADC channels, count nch ──────
                struct ChResult {
                    int     mod_id;
                    uint8_t mod_type;
                    int     npeaks;
                    float   peak_integral;
                };
                std::vector<ChResult> channels;
                channels.reserve(1500);

                for (int r = 0; r < event->nrocs; ++r) {
                    auto &roc = event->rocs[r];
                    if (!roc.present) continue;
                    auto cit = roc_to_crate.find(roc.tag);
                    int crate = (cit != roc_to_crate.end()) ? cit->second : roc.tag;
                    for (int s = 0; s < fdec::MAX_SLOTS; ++s) {
                        if (!roc.slots[s].present) continue;
                        for (int c = 0; c < 16; ++c) {
                            if (!(roc.slots[s].channel_mask & (1ull << c))) continue;
                            auto &cd = roc.slots[s].channels[c];
                            if (cd.nsamples <= 0) continue;
                            int mid = replay.moduleID(crate, s, c);
                            if (mid < 0) continue;
                            auto mtype = static_cast<uint8_t>(replay.moduleType(crate, s, c));
                            ana.SetChannelKey(roc.tag, s, c);
                            ana.Analyze(cd.samples, cd.nsamples, wres);
                            //temporary test for integral
                            for(int s = wres.peaks[0].left -2; s < wres.peaks[0].left; s++)
                                wres.peaks[0].integral += cd.samples[s] - wres.ped.mean;
                            for(int s = wres.peaks[0].right + 1; s <= wres.peaks[0].right + 3; s++)
                                wres.peaks[0].integral += cd.samples[s] - wres.ped.mean;
                            channels.push_back({mid, mtype,
                                                wres.npeaks,
                                                wres.npeaks > 0 ? wres.peaks[0].integral : 0.f});
                        }
                    }
                }

                // ── classify event (same condition as Replay::Process) ────
                int nch = static_cast<int>(channels.size());
                uint32_t trigger_bits = event->info.trigger_bits;
                bool is_lms   = ((trigger_bits & (1u << 24)) != 0 && nch > 1000);
                bool is_alpha = ((trigger_bits & (1u << 25)) != 0 && nch < 50);
                if (!is_lms && !is_alpha) { ++total; continue; }

                // ── fill histograms ───────────────────────────────────────
                for (const auto &cr : channels) {
                    if (cr.npeaks != 1) continue;

                    if (is_lms && cr.mod_type == static_cast<uint8_t>(prad2::MOD_PbWO4)) {
                        int wid = cr.mod_id - 1000;   // W-module 1-based ID
                        if (wid >= 1 && wid <= 1156)
                            mod_lms[wid - 1]->Fill(cr.peak_integral);
                    }
                    if (cr.mod_type == static_cast<uint8_t>(prad2::MOD_LMS)) {
                        int lms_id = cr.mod_id - 3100; // LMS1=3101→1, LMS2→2, LMS3→3
                        if (lms_id >= 1 && lms_id <= 3) {
                            if (is_lms)   ref_lms  [lms_id - 1]->Fill(cr.peak_integral);
                            if (is_alpha) ref_alpha [lms_id - 1]->Fill(cr.peak_integral);
                        }
                    }
                }
                ++total;
                if (total % 1000 == 0)
                    std::cerr << "\r  " << total << " events" << std::flush;
            }
        }
        ch.Close();
    }
    std::cerr << "\n  Total events processed: " << total << "\n";

    // ── fit histograms (gain_hist_fitter, same as Replay::Process) ────────
    prad2::FitResult fit_ref_lms[3], fit_ref_alpha[3], fit_W_lms[1156];
    float refPMT_ratio[3] = {1.f, 1.f, 1.f};
    float gain_W[1156][3] = {};

    for (int i = 0; i < 3; ++i) {
        fit_ref_lms  [i] = prad2::gain_hist_fitter(ref_lms  [i], 0.5f);
        fit_ref_alpha[i] = prad2::gain_hist_fitter(ref_alpha[i], 0.5f);
        if (fit_ref_lms[i].mean > 0.f && fit_ref_alpha[i].mean > 0.f)
            refPMT_ratio[i] = fit_ref_lms[i].mean / fit_ref_alpha[i].mean;
        std::cerr << Form("  LMS%d : lms=%.1f  alpha=%.1f  ratio=%.4f\n",
                          i+1, fit_ref_lms[i].mean, fit_ref_alpha[i].mean, refPMT_ratio[i]);
    }
    for (int i = 0; i < 1156; ++i) {
        fit_W_lms[i] = prad2::gain_hist_fitter(mod_lms[i], 0.5f);
        for (int j = 0; j < 3; ++j)
            gain_W[i][j] = (refPMT_ratio[j] > 0.f && fit_W_lms[i].mean > 0.f)
                         ? fit_W_lms[i].mean / refPMT_ratio[j] : 0.f;
    }

    // ── write .dat output ─────────────────────────────────────────────────
    // Format: Name  lms_peak  lms_sigma  lms_chi2/ndf  g1  g2  g3
    // LMS rows store (lms_peak lms_sigma chi2 alpha_peak alpha_sigma alpha_chi2)
    // as informational columns; LoadGainFactors() silently skips them.
    {
        std::ofstream out(output_dat);
        if (!out.is_open()) {
            std::cerr << "Cannot open output file: " << output_dat << "\n";
            return 1;
        }
        out << std::left;
        for (int i = 0; i < 3; ++i) {
            out << std::setw(8) << ("LMS" + std::to_string(i + 1))
                << std::setw(12) << std::fixed << std::setprecision(3) << fit_ref_lms[i].mean
                << std::setw(12) << fit_ref_lms[i].sigma
                << std::setw(12) << fit_ref_lms[i].chi2pndf
                << std::setw(12) << fit_ref_alpha[i].mean
                << std::setw(12) << fit_ref_alpha[i].sigma
                << std::setw(12) << fit_ref_alpha[i].chi2pndf
                << "\n";
        }
        int n_mod = 0;
        for (int i = 0; i < 1156; ++i) {
            if (fit_W_lms[i].mean <= 0.f) continue;
            out << std::setw(8) << ("W" + std::to_string(i + 1))
                << std::setw(12) << fit_W_lms[i].mean
                << std::setw(12) << fit_W_lms[i].sigma
                << std::setw(12) << fit_W_lms[i].chi2pndf
                << std::setw(12) << gain_W[i][0]
                << std::setw(12) << gain_W[i][1]
                << std::setw(12) << gain_W[i][2]
                << "\n";
            ++n_mod;
        }
        std::cerr << "Gain factors written to " << output_dat
                  << " (" << n_mod << " W modules)\n";
    }

    // ── save histograms to ROOT file ──────────────────────────────────────
    {
        TFile *outfile = TFile::Open(output_root.c_str(), "RECREATE");
        if (outfile && outfile->IsOpen()) {
            outfile->mkdir("lms");
            outfile->cd("lms");
            for (int i = 0; i < 3; ++i) { ref_lms[i]->Write(); ref_alpha[i]->Write(); }
            outfile->mkdir("modules");
            outfile->cd("modules");
            for (int i = 0; i < 1156; ++i) mod_lms[i]->Write();
            outfile->Close();
            std::cerr << "Histograms saved to " << output_root << "\n";
        }
    }

    for (int i = 0; i < 3;    ++i) { delete ref_lms[i]; delete ref_alpha[i]; }
    for (int i = 0; i < 1156; ++i)   delete mod_lms[i];
    return 0;
}