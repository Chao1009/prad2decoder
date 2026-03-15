#pragma once

// #include "TObject.h"

#define FADC250_MAX_NPEAKS 4
#define FADC250_MAX_NSAMPLES 256

// data structures
namespace fdec
{

class Peak {
public:
    double height, integral, time;
    uint32_t pos, left, right;
    bool overflow;

    Peak(double h = 0., double i = 0., double t = 0., uint32_t p = 0, uint32_t l = 0, uint32_t r = 0, bool o = false)
        : pos(p), left(l), right(r), height(h), integral(i), time(t), overflow(o)
    {
        // place holder
    }

    bool Inside(uint32_t i) {
        return (i >= pos - left) && (i <= pos + right);
    }

//    ClassDef(Peak, 1);
};

class Pedestal {
public:
    double mean, err;

    Pedestal(double m = 0., double e = 0.) : mean(m), err(e) {}

//    ClassDef(Pedestal, 1);
};

class Fadc250Data
{
public:
    Pedestal ped;
    std::vector<Peak> peaks;
    std::vector<uint32_t> raw;

    Fadc250Data(): ped(0., 0.)
    {
        peaks.reserve(FADC250_MAX_NPEAKS);
        raw.reserve(FADC250_MAX_NSAMPLES);
    }

    void Clear() { ped = Pedestal(0., 0.), peaks.clear(), raw.clear(); }

//    ClassDef(Fadc250Data, 1);  // root io
};

}; // namespace fdec

