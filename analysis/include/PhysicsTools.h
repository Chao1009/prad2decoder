#pragma once
//=============================================================================
// PhysicsTools.h — physics analysis tools for PRad2
//
// Provides kinematic calculations, energy loss corrections, and
// per-module energy histogram management with ROOT.
// Depends on prad2ana (HyCalSystem) and ROOT (TH1F/TH2F).
//=============================================================================

#include "HyCalSystem.h"
#include <TH1F.h>
#include <TH2F.h>
#include <array>
#include <string>
#include <vector>
#include <memory>

namespace analysis {

class PhysicsTools
{
public:
    explicit PhysicsTools(fdec::HyCalSystem &hycal);
    ~PhysicsTools();

    // --- per-module cluster energy histograms --------------------------------
    void FillModuleEnergy(int module_index, float energy);
    TH1F *GetModuleHist(int module_index) const;

    // --- 2D energy vs module index -------------------------------------------
    void FillEnergyVsModule(int module_index, float energy);
    TH2F *GetEnergyVsModuleHist() const { return h2_energy_module_.get(); }

    // --- peak / resolution analysis ------------------------------------------
    // Returns {peak, resolution} from Gaussian fit. Resolution = sigma/mean.
    std::array<float, 2> FitPeakResolution(int module_index) const;

    // --- kinematics ----------------------------------------------------------
    // Expected energy for elastic e-p or e-e scattering.
    //   theta: scattering angle in degrees
    //   Ebeam: beam energy in MeV
    //   type:  "ep" or "ee"
    static float ExpectedEnergy(float theta_deg, float Ebeam, const std::string &type);

    // Energy loss correction for electron passing through target + windows.
    //   theta: scattering angle in degrees
    //   E:     measured energy in MeV
    static float EnergyLoss(float theta_deg, float E);

private:
    fdec::HyCalSystem &hycal_;
    std::vector<std::unique_ptr<TH1F>> module_hists_;  // one per module
    std::unique_ptr<TH2F> h2_energy_module_;
};

} // namespace analysis
