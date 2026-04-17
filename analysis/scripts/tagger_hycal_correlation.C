//============================================================================
// tagger_hycal_correlation.C — tagger TDC → HyCal coincidence study
//
// Event-wise loop:
//   1. Read each physics event.  If T10R doesn't fire, skip.
//   2. For each Eₓ ∈ {E49..E53} that also fires, fill ΔT = tdc(T10R) − tdc(Eₓ).
//   3. If the event also has W1156 FADC samples and the pair's ΔT sits
//      within ±Nσ of its hard-coded peak μ, fill W1156_height_Eₓ and
//      W1156_integral_Eₓ.  Each pair is tested independently per event,
//      so one event can feed multiple pairs.
//
// The μ / σ values below were obtained from an earlier fit pass — edit them
// here if a later run needs different timing windows.  Set NSIGMA_CUT to
// tune the cut width.
//
// Compile with ACLiC after loading rootlogon:
//
//     cd build
//     root -l ../analysis/scripts/rootlogon.C
//     .x ../analysis/scripts/tagger_hycal_correlation.C+( \
//         "/data/stage6/prad_023686/prad_023686.evio.00000", \
//         "tagger_w1156_corr.root", 500000)
//
// Or one-liner:
//
//     root -l -b -q analysis/scripts/rootlogon.C \
//         'analysis/scripts/tagger_hycal_correlation.C+("path.evio","out.root",0)'
//============================================================================

#include "EvChannel.h"
#include "DaqConfig.h"
#include "load_daq_config.h"
#include "Fadc250Data.h"
#include "SspData.h"
#include "VtpData.h"
#include "TdcData.h"

#include <TCanvas.h>
#include <TFile.h>
#include <TH1D.h>
#include <TH1F.h>
#include <TLine.h>
#include <TString.h>
#include <TStyle.h>
#include <TSystem.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

using namespace evc;

//-----------------------------------------------------------------------------
// Configuration — adjust if the DAQ layout or fit results change
//-----------------------------------------------------------------------------
namespace {

// Tagger crate: one V1190 per ROC, TDC data under bank 0xE107.
constexpr int TAGGER_SLOT = 18;
constexpr int T10R_CH     = 0;

// Coincidence pairs.  mu / sigma are the Gaussian fit results from the
// previous analysis pass (LSB units).  Update if the run changes.
struct PairCfg {
    const char *name;
    int         channel;   // V1190 channel in TAGGER_SLOT
    double      mu;        // coincidence peak centre [LSB]
    double      sigma;     // peak width              [LSB]
};
constexpr PairCfg PAIRS[] = {
    { "E49", 11,  2692.24,  31.12 },
    { "E50", 12,  2935.62, 223.35 },
    { "E51", 13,  2852.43,  40.14 },
    { "E52", 14,  2841.13,  71.80 },
    { "E53", 15,  2786.88,  82.53 },
};
constexpr int N_PAIRS = sizeof(PAIRS) / sizeof(PAIRS[0]);

// Timing cut: |dt - mu| < NSIGMA_CUT * sigma.
constexpr double NSIGMA_CUT = 3.0;

// HyCal module W1156 DAQ address (from database/daq_map.json:
// {"W1156", "crate":6, "slot":7, "channel":3}; crate 6 → ROC 0x8C).
constexpr uint32_t W1156_ROC  = 0x8C;
constexpr int      W1156_SLOT = 7;
constexpr int      W1156_CH   = 3;

// Simple FADC peak finder — pedestal and integration window.
constexpr int PED_WINDOW    = 10;
constexpr int INT_HALFWIDTH = 8;

// ΔT histogram axis.  Centred on each pair's μ with ±DT_HALF span and
// DT_BINS bins.  (±500 LSB ≈ ±12.5 ns — plenty for the observed σ's
// including the wide E50 peak.)
constexpr int    DT_BINS = 400;
constexpr double DT_HALF = 500.0;

// W1156 output histograms.
constexpr int    H_BINS = 200;
constexpr double H_MIN  = 0.0;
constexpr double H_MAX  = 4000.0;
constexpr int    I_BINS = 200;
constexpr double I_MIN  = 0.0;
constexpr double I_MAX  = 40000.0;

//-----------------------------------------------------------------------------
// Helpers
//-----------------------------------------------------------------------------

// Earliest hit (smallest TDC value) for (slot, ch) in this event, or -1.
static int first_tdc(const tdc::TdcEventData &t, int slot, int ch)
{
    int best = -1;
    for (int i = 0; i < t.n_hits; ++i) {
        const auto &h = t.hits[i];
        if ((int)h.slot != slot || (int)h.channel != ch) continue;
        if (best < 0 || (int)h.value < best) best = (int)h.value;
    }
    return best;
}

// Simple FADC peak finder. Returns false if the channel has no samples.
static bool hycal_peak(const fdec::ChannelData &c, float &height, float &integral)
{
    if (c.nsamples <= PED_WINDOW) return false;
    double ped = 0.0;
    for (int i = 0; i < PED_WINDOW; ++i) ped += c.samples[i];
    ped /= PED_WINDOW;

    int tmax = 0;
    double maxv = (double)c.samples[0] - ped;
    for (int i = 1; i < c.nsamples; ++i) {
        double v = (double)c.samples[i] - ped;
        if (v > maxv) { maxv = v; tmax = i; }
    }
    height = (float)maxv;

    int lo = std::max(0, tmax - INT_HALFWIDTH);
    int hi = std::min<int>(c.nsamples, tmax + INT_HALFWIDTH + 1);
    double sum = 0.0;
    for (int i = lo; i < hi; ++i) sum += (double)c.samples[i] - ped;
    integral = (float)sum;
    return true;
}

// Pull W1156 peak (height, integral). Returns false if absent.
static bool w1156_peak(const fdec::EventData &evt,
                       float &height, float &integral)
{
    const fdec::RocData *roc = evt.findRoc(W1156_ROC);
    if (!roc) return false;
    const fdec::SlotData &s = roc->slots[W1156_SLOT];
    if (!s.present) return false;
    const fdec::ChannelData &c = s.channels[W1156_CH];
    if (c.nsamples <= 0) return false;
    return hycal_peak(c, height, integral);
}

} // anonymous namespace

//=============================================================================
// Entry point
//=============================================================================

int tagger_hycal_correlation(const char *evio_path,
                             const char *out_path   = "tagger_w1156_corr.root",
                             Long64_t    max_events = 0,
                             const char *daq_config = nullptr)
{
    //---- load DAQ config ----------------------------------------------------
    std::string cfg_path = daq_config ? daq_config : "";
    if (cfg_path.empty()) {
        const char *db = std::getenv("PRAD2_DATABASE_DIR");
        cfg_path = std::string(db ? db : "database") + "/daq_config.json";
    }

    DaqConfig cfg;
    if (!load_daq_config(cfg_path, cfg)) {
        std::cerr << "ERROR: cannot load " << cfg_path << "\n";
        return 1;
    }

    //---- open evio ----------------------------------------------------------
    EvChannel ch;
    ch.SetConfig(cfg);
    if (ch.Open(evio_path) != status::success) {
        std::cerr << "ERROR: cannot open " << evio_path << "\n";
        return 1;
    }

    //---- pre-create output histograms (one set per pair) --------------------
    TFile out(out_path, "RECREATE");
    gStyle->SetOptStat(1110);

    std::vector<TH1D*> h_dt(N_PAIRS, nullptr);
    std::vector<TH1F*> h_h (N_PAIRS, nullptr);
    std::vector<TH1F*> h_i (N_PAIRS, nullptr);

    for (int k = 0; k < N_PAIRS; ++k) {
        const auto &p = PAIRS[k];
        const double half = NSIGMA_CUT * p.sigma;

        h_dt[k] = new TH1D(
            TString::Format("dt_T10R_%s", p.name),
            TString::Format("#DeltaT = T10R - %s   "
                            "[cut: |#DeltaT - %.1f| < %.1f#sigma (%.1f)];"
                            "tdc(T10R) - tdc(%s) [LSB];events",
                            p.name, p.mu, NSIGMA_CUT, half, p.name),
            DT_BINS, p.mu - DT_HALF, p.mu + DT_HALF);

        h_h[k] = new TH1F(
            TString::Format("W1156_height_%s", p.name),
            TString::Format("W1156 peak height, selected by T10R-%s "
                            "|#DeltaT - %.1f| < %.1f LSB;"
                            "height [ADC];events", p.name, p.mu, half),
            H_BINS, H_MIN, H_MAX);
        h_i[k] = new TH1F(
            TString::Format("W1156_integral_%s", p.name),
            TString::Format("W1156 peak integral, selected by T10R-%s "
                            "|#DeltaT - %.1f| < %.1f LSB;"
                            "integral [ADC#upoint sample];events", p.name, p.mu, half),
            I_BINS, I_MIN, I_MAX);
    }

    //---- single event-wise pass --------------------------------------------
    auto event_ptr = std::make_unique<fdec::EventData>();
    auto tdc_ptr   = std::make_unique<tdc::TdcEventData>();
    auto &event   = *event_ptr;
    auto &tdc_evt = *tdc_ptr;

    Long64_t n_physics = 0;
    std::vector<Long64_t> n_dt (N_PAIRS, 0);
    std::vector<Long64_t> n_sel(N_PAIRS, 0);
    Long64_t n_w1156_events = 0;

    std::cout << "reading " << evio_path << std::endl;

    while (ch.Read() == status::success) {
        if (!ch.Scan() || ch.GetEventType() != EventType::Physics) continue;

        const int nsub = ch.GetNEvents();
        for (int i = 0; i < nsub; ++i) {
            ch.DecodeEvent(i, event, nullptr, nullptr, &tdc_evt);

            int t0 = first_tdc(tdc_evt, TAGGER_SLOT, T10R_CH);
            if (t0 < 0) continue;

            // W1156 is optional per event; if absent, we still fill the
            // ΔT plots but skip the W1156-selected histograms.
            float w_h = 0.f, w_i = 0.f;
            bool have_w = w1156_peak(event, w_h, w_i);
            if (have_w) ++n_w1156_events;

            // Test every pair independently against its own cut.
            for (int k = 0; k < N_PAIRS; ++k) {
                int te = first_tdc(tdc_evt, TAGGER_SLOT, PAIRS[k].channel);
                if (te < 0) continue;
                const double dt = (double)t0 - (double)te;
                h_dt[k]->Fill(dt);
                ++n_dt[k];

                if (!have_w) continue;
                const double half = NSIGMA_CUT * PAIRS[k].sigma;
                if (std::fabs(dt - PAIRS[k].mu) >= half) continue;
                h_h[k]->Fill(w_h);
                h_i[k]->Fill(w_i);
                ++n_sel[k];
            }

            ++n_physics;
            if (n_physics % 100000 == 0)
                std::cout << "  " << n_physics << " physics events processed"
                          << " (W1156 seen so far: " << n_w1156_events << ")\n";
            if (max_events > 0 && n_physics >= max_events) goto done;
        }
    }
done:
    ch.Close();
    std::cout << "done: " << n_physics << " physics events"
              << " (W1156 present in " << n_w1156_events << ")\n";

    //---- write ΔT + W1156 histograms, build the summary canvas --------------
    TCanvas *canvas = new TCanvas("summary", "tagger-W1156 correlations",
                                  1500, 900);
    canvas->Divide(N_PAIRS, 3);

    for (int k = 0; k < N_PAIRS; ++k) {
        const auto &p = PAIRS[k];
        h_dt[k]->Write();
        h_h [k]->Write();
        h_i [k]->Write();

        // Draw cut lines at μ ± Nσ on the ΔT plot of the summary canvas.
        canvas->cd(k + 1);
        h_dt[k]->Draw();
        const double half = NSIGMA_CUT * p.sigma;
        auto *l_lo = new TLine(p.mu - half, 0, p.mu - half, h_dt[k]->GetMaximum());
        auto *l_hi = new TLine(p.mu + half, 0, p.mu + half, h_dt[k]->GetMaximum());
        l_lo->SetLineColor(kRed); l_lo->SetLineStyle(2); l_lo->Draw("same");
        l_hi->SetLineColor(kRed); l_hi->SetLineStyle(2); l_hi->Draw("same");

        canvas->cd(k + 1 + N_PAIRS);      h_h[k]->Draw();
        canvas->cd(k + 1 + 2 * N_PAIRS);  h_i[k]->Draw();
    }
    canvas->Write();
    out.Close();

    //---- terminal summary --------------------------------------------------
    std::cout << "\n=== Summary (hard-coded "
              << NSIGMA_CUT << "-sigma cuts) ===\n";
    std::cout << "   pair   mu[LSB]  sigma[LSB]   cut-half[LSB]   "
                 "n_dt_filled   n_w1156_selected   keep\n";
    for (int k = 0; k < N_PAIRS; ++k) {
        const auto &p = PAIRS[k];
        double half = NSIGMA_CUT * p.sigma;
        double frac = n_dt[k] ? 100.0 * (double)n_sel[k] / n_dt[k] : 0.0;
        std::cout << "  T10R-" << p.name
                  << "  " << std::setw(8) << p.mu
                  << "   " << std::setw(8) << p.sigma
                  << "   " << std::setw(10) << half
                  << "   " << std::setw(10) << n_dt[k]
                  << "   " << std::setw(15) << n_sel[k]
                  << "   " << std::setw(5) << frac << "%\n";
    }
    std::cout << "\nhistograms written to " << out_path << "\n"
              << "cut lines are overlaid on each ΔT panel of the summary "
                 "canvas.\n";
    return 0;
}
