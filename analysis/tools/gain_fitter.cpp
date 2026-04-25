#include <TFile.h>
#include <TKey.h>
#include <TH1F.h>
#include <TF1.h>
#include <TH2F.h>
#include <TGraph.h>
#include <TCanvas.h>
#include <TSystem.h>
#include <TSystemDirectory.h>
#include <TString.h>
#include <TStyle.h>
#include <TLegend.h>
#include <TROOT.h>

#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <fstream>
#include <iostream>
#include <cmath>
#include <regex>
#include <string>
#include <stdexcept>
#include <cctype>
#include <iomanip>

struct FitResult{
    float mean     = 0.;
    float sigma    = 0.;
    float chi2pndf = 0.;
};

FitResult FitHistogramWithGaussian(TH1F* h, const float & fac);
std::string GetChannelName(const std::string &histName);

int main(int argc, char *argv[])
{   
    std::string run_number = "";
    std::string fileDir = ".";
    int opt;
    while ((opt = getopt(argc, argv, "r:s:e:d:")) != -1) {
        switch (opt) {
            case 'r': run_number    = optarg; break;
            case 'd': fileDir       = optarg; break;
        }
    }
    
    TFile* f = new TFile(Form("%s/prad_%s_LMS.root", fileDir.c_str(), run_number.c_str()), "READ");
    if (!f || f->IsZombie()) {
        std::cerr << "ERROR: cannot open ROOT file\n";
        return 1;
    }

    std::vector<TH1F*> hists;
    std::vector<FitResult> results;
    std::vector<std::string> name;
    TIter next(f->GetListOfKeys());
    TKey *key;
    while ((key = (TKey*)next())) {
        if (std::string(key->GetClassName()) != "TH1F") continue;
        TH1F *h = (TH1F*)key->ReadObj();
        if (!h) continue;
        
        results.push_back(FitHistogramWithGaussian(h, 0.1));
        name.push_back(GetChannelName(h->GetName()));
        hists.push_back(h);
    }
    printf("Loaded %zu TH1F histograms\n", hists.size());

    TFile* outRoot = new TFile(Form("%s/prad_%s_LMS_fitted.root", fileDir.c_str(), run_number.c_str()), "RECREATE");
    outRoot->cd();
    
    for (auto h : hists) h->Write();
    
    outRoot->Close();
    f->Close();
    
    //write gain factors to dat file
    //format: first 3 line reference channel name, alpha peak position, alpha sigma, alpha fit chi2/ndf, lms peak position, lms sigma, lms fit chi2/ndf
    //format: the rest: HyCal module name, lms peak, lms sigma, lms fit chi2/ndf, and three gain factors using 3 reference PMT
    
    std::ofstream outDatFile;
    outDatFile.open(Form("%s/prad_%s_LMS.dat", fileDir.c_str(), run_number.c_str()));
    
    
    for (unsigned int i=0; i<3; i++){
        outDatFile<<std::setw(9)<<Form("LMS%d", i+1)
                  <<std::setw(15)<<results[i].mean<<std::setw(15)<<results[i].sigma<<std::setw(15)<<results[i].chi2pndf
                  <<std::setw(15)<<results[i+3].mean<<std::setw(15)<<results[i+3].sigma<<std::setw(15)<<results[i+3].chi2pndf<<std::endl;
    }
    
    for (unsigned int i = 6; i < hists.size(); i++){
        float factor[3] = {0., 0., 0.};
        for (int j = 0; j<3; j++){
            if (results[j].mean > 1. && results[j+3].mean > 1.)
                factor[j] = results[i].mean * results[j].mean / results[j+3].mean;
            if (!std::isfinite(factor[j]))
                factor[j] = 0.f;
        }
        outDatFile<<std::setw(9)<<name[i]
                  <<std::setw(15)<<results[i].mean<<std::setw(15)<<results[i].sigma<<std::setw(15)<<results[i].chi2pndf
                  <<std::setw(15)<<factor[0]<<std::setw(15)<<factor[1]<<std::setw(15)<<factor[2]<<std::endl;
    }
    outDatFile.close();

    return 0;
}

std::string GetChannelName(const std::string &histName)
{
    auto pos = histName.find('_');
    if (pos == std::string::npos)
        return histName;
    return histName.substr(0, pos);
}

FitResult FitHistogramWithGaussian(TH1F* h, const float & fac)
{
    FitResult result;

    if (!h) {
        return result;
    }

    const int nBins = h->GetNbinsX();
    if (nBins <= 0) {
        return result;
    }

    const int maxBin = h->GetMaximumBin();
    const double maxContent = h->GetBinContent(maxBin);

    if (maxContent <= 0.) {
        return result;
    }

    const double threshold = fac * maxContent;

    int leftBin  = maxBin;
    int rightBin = maxBin;

    // Find first bin to the left below 10% of max
    for (int ibin = maxBin; ibin >= 1; --ibin) {
        if (h->GetBinContent(ibin) < threshold) {
            leftBin = ibin;
            break;
        }
        if (ibin == 1) {
            leftBin = 1;
        }
    }

    // Find first bin to the right below 10% of max
    for (int ibin = maxBin; ibin <= nBins; ++ibin) {
        if (h->GetBinContent(ibin) < threshold) {
            rightBin = ibin;
            break;
        }
        if (ibin == nBins) {
            rightBin = nBins;
        }
    }

    // Optional: if you want the fit range to stay inside the above-threshold region,
    // shift inward by one bin when the threshold-crossing bin itself is below threshold.
    if (leftBin < maxBin && h->GetBinContent(leftBin) < threshold) {
        leftBin++;
    }
    if (rightBin > maxBin && h->GetBinContent(rightBin) < threshold) {
        rightBin--;
    }

    // Ensure at least 5 bins around the maximum are included so that a
    // 3-parameter Gaussian fit is always well-constrained, even when the
    // peak is very narrow (only a few filled bins).
    if (leftBin  > maxBin - 2) leftBin  = maxBin - 2;
    if (rightBin < maxBin + 2) rightBin = maxBin + 2;

    // Safety check
    if (leftBin < 1) leftBin = 1;
    if (rightBin > nBins) rightBin = nBins;
    if (leftBin >= rightBin) {
        return result;
    }
    
    const double xLow  = h->GetXaxis()->GetBinLowEdge(leftBin);
    const double xHigh = h->GetXaxis()->GetBinUpEdge(rightBin);

    const double peakX = h->GetXaxis()->GetBinCenter(maxBin);

    // A reasonable initial sigma guess from fit window width
    double sigmaGuess = 0.5 * (xHigh - xLow) / 2.0;
    if (sigmaGuess <= 0.) {
        sigmaGuess = h->GetRMS();
    }
    if (sigmaGuess <= 0.) {
        sigmaGuess = h->GetBinWidth(maxBin);
    }

    std::string fitName = std::string(h->GetName()) + "_gaus_fit";
    TF1 * gausFit = new TF1(fitName.c_str(), "gaus", xLow, xHigh);
    gausFit->SetParameters(maxContent, peakX, sigmaGuess);

    // R = fit in function range, Q = quiet, N = do not store function in histogram
    // "N" is required to avoid double-ownership: new TF1 is registered in gROOT's
    // global list; without "N", Fit() also stores it in the histogram's list,
    // causing a double-free during ROOT cleanup at program exit.
    int fitStatus = h->Fit(gausFit, "RQN");

    if (fitStatus != 0) {
        delete gausFit;
        return result;
    }

    const float mean  = static_cast<float>(gausFit->GetParameter(1));
    const float sigma = static_cast<float>(gausFit->GetParameter(2));
    if (!std::isfinite(mean) || !std::isfinite(sigma) || sigma <= 0.f) {
        delete gausFit;
        return result;
    }
    result.mean  = mean;
    result.sigma = sigma;

    const double ndf = gausFit->GetNDF();
    if (ndf > 0) {
        result.chi2pndf = static_cast<float>(gausFit->GetChisquare() / ndf);
    }

    // Transfer ownership from gROOT's global list to the histogram so the fit
    // curve is saved in the output file and there is only one owner (no double-free).
    gROOT->GetListOfFunctions()->Remove(gausFit);
    h->GetListOfFunctions()->Add(gausFit);

    return result;
}
