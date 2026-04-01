#pragma once
// =========================================================================
// root_data_source.h — ROOT file data sources (requires WITH_ROOT)
//
// RootRawDataSource:   reads "events" tree from replay_rawdata output
// RootReconDataSource: reads "recon" tree from replay_recon output
//
// Uses shared data structs from ReplayData.h — any schema change there
// automatically updates both the writer (Replay) and these readers.
// =========================================================================

#ifdef WITH_ROOT

#include "data_source.h"
#include "EventData.h"

#include <TFile.h>
#include <TTree.h>

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>

// ── ROOT raw replay data source ──────────────────────────────────────────

class RootRawDataSource : public DataSource {
public:
    explicit RootRawDataSource(const std::unordered_map<int, uint32_t> &crate_to_roc)
        : crate_to_roc_(crate_to_roc) {}

    std::string open(const std::string &path) override;
    void close() override;
    DataSourceCaps capabilities() const override;
    int eventCount() const override { return n_entries_; }
    std::string decodeEvent(int index, fdec::EventData &evt,
                             ssp::SspEventData *ssp = nullptr) override;
    void iterateAll(EventCallback ev_cb, ReconCallback recon_cb,
                    ControlCallback ctrl_cb, EpicsCallback epics_cb) override;

private:
    std::unordered_map<int, uint32_t> crate_to_roc_;
    std::unique_ptr<TFile> file_;
    TTree *tree_ = nullptr;
    int n_entries_ = 0;
    bool has_peaks_ = false;
    std::mutex mtx_;

    // branch read buffer — uses the shared struct from ReplayData.h
    prad2::RawEventData ev_;

    void fillEventData(fdec::EventData &evt) const;
};

// ── ROOT recon data source ───────────────────────────────────────────────

class RootReconDataSource : public DataSource {
public:
    std::string open(const std::string &path) override;
    void close() override;
    DataSourceCaps capabilities() const override;
    int eventCount() const override { return n_entries_; }
    std::string decodeEvent(int index, fdec::EventData &evt,
                             ssp::SspEventData *ssp = nullptr) override;
    bool decodeReconEvent(int index, ReconEventData &recon) override;
    void iterateAll(EventCallback ev_cb, ReconCallback recon_cb,
                    ControlCallback ctrl_cb, EpicsCallback epics_cb) override;

private:
    std::unique_ptr<TFile> file_;
    TTree *tree_ = nullptr;
    int n_entries_ = 0;
    std::mutex mtx_;

    // branch read buffer — uses the shared struct from ReplayData.h
    prad2::ReconEventData ev_;

    void fillRecon(ReconEventData &recon) const;
};

// Factory helper: detect tree type and return appropriate source.
std::unique_ptr<DataSource> createRootDataSource(
    const std::string &path,
    const std::unordered_map<int, uint32_t> &crate_to_roc);

#endif // WITH_ROOT
