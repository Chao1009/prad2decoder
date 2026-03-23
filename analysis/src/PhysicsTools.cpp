//=============================================================================
// PhysicsTools.cpp — physics analysis tools
//=============================================================================

#include "PhysicsTools.h"
#include <TF1.h>
#include <cmath>

namespace analysis {

// Physical constants
static constexpr float M_PROTON  = 938.272f;   // MeV
static constexpr float M_ELECTRON = 0.511f;    // MeV
static constexpr float DEG2RAD = 3.14159265f / 180.f;

PhysicsTools::PhysicsTools(fdec::HyCalSystem &hycal)
    : hycal_(hycal)
{
    int nmod = hycal_.module_count();
    module_hists_.resize(nmod);
    for (int i = 0; i < nmod; ++i) {
        auto &mod = hycal_.module(i);
        std::string name = "h_" + mod.name;
        std::string title = mod.name + " cluster energy;Energy (MeV);Counts";
        module_hists_[i] = std::make_unique<TH1F>(name.c_str(), title.c_str(), 300, 0, 3000);
    }
    h2_energy_module_ = std::make_unique<TH2F>(
        "h2_energy_module", "Energy vs Module;Module Index;Energy (MeV)",
        nmod, 0, nmod, 300, 0, 3000);
}

PhysicsTools::~PhysicsTools() = default;

void PhysicsTools::FillModuleEnergy(int module_index, float energy)
{
    if (module_index >= 0 && module_index < (int)module_hists_.size())
        module_hists_[module_index]->Fill(energy);
}

TH1F *PhysicsTools::GetModuleHist(int module_index) const
{
    if (module_index >= 0 && module_index < (int)module_hists_.size())
        return module_hists_[module_index].get();
    return nullptr;
}

void PhysicsTools::FillEnergyVsModule(int module_index, float energy)
{
    if (h2_energy_module_)
        h2_energy_module_->Fill(module_index, energy);
}

std::array<float, 2> PhysicsTools::FitPeakResolution(int module_index) const
{
    if (module_index < 0 || module_index >= (int)module_hists_.size())
        return {0.f, 0.f};

    TH1F *h = module_hists_[module_index].get();
    if (!h || h->GetEntries() < 50) return {0.f, 0.f};

    // find peak bin, fit Gaussian around it
    int maxbin = h->GetMaximumBin();
    float peak = h->GetBinCenter(maxbin);
    float rms  = h->GetRMS();

    TF1 gaus("gfit", "gaus", peak - 2 * rms, peak + 2 * rms);
    h->Fit(&gaus, "QNR");

    float mean  = gaus.GetParameter(1);
    float sigma = gaus.GetParameter(2);
    float resolution = (mean > 0) ? sigma / mean : 0.f;
    return {mean, resolution};
}

float PhysicsTools::ExpectedEnergy(float theta_deg, float Ebeam, const std::string &type)
{
    float theta = theta_deg * DEG2RAD;
    float cos_t = std::cos(theta);
    float sin_t = std::sin(theta);

    if (type == "ep") {
        // elastic e-p: E' = E * M / (M + E*(1 - cos_t))
        // where M = proton mass
        return Ebeam * M_PROTON / (M_PROTON + Ebeam * (1.f - cos_t));
    }
    if (type == "ee") {
        // Moller scattering: E' = E * cos^2(theta) / (1 + (E/m)(sin^2(theta)))
        // simplified from CM frame kinematics
        float gamma = Ebeam / M_ELECTRON;
        float num = (gamma + 1.f) * cos_t * cos_t;
        float den = (gamma + 1.f) - (gamma - 1.f) * cos_t * cos_t;
        if (den <= 0) return 0.f;
        return M_ELECTRON * num / den;
    }
    return 0.f;
}

float PhysicsTools::EnergyLoss(float theta_deg, float E)
{
    // simplified energy loss through target materials
    // path lengths scale as 1/cos(theta) for small angles
    float theta = theta_deg * DEG2RAD;
    float sec = (std::cos(theta) > 0.01f) ? (1.f / std::cos(theta)) : 100.f;

    // material thicknesses (mm) and dE/dx (MeV/mm) — approximate values
    // aluminum window: 0.025 mm, dE/dx ~ 1.6 MeV/mm
    // GEM foils: ~0.05 mm effective, dE/dx ~ 2.0 MeV/mm
    // kapton window: ~0.05 mm, dE/dx ~ 1.8 MeV/mm
    float eloss = 0.f;
    eloss += 0.025f * 1.6f * sec;  // Al window
    eloss += 0.050f * 2.0f * sec;  // GEM
    eloss += 0.050f * 1.8f * sec;  // kapton cover

    return eloss;  // total energy loss in MeV
}

} // namespace analysis
