#pragma once
//=============================================================================
// DetectorTransform.h — planar detector coordinate transform
//
// Transforms detector-plane coordinates (x, y) to lab frame via
// Euler rotation (Rx * Ry * Rz) then translation.
// Reusable for HyCal, GEMs, or any planar detector.
//
// The rotation matrix is lazily computed on first use and cached.
// Call prepare() explicitly to force precomputation, or just use
// toLab()/rotate() — they auto-prepare if needed.
//=============================================================================

#include <cmath>

struct DetectorTransform {
    float x=0, y=0, z=0;               // detector origin in lab frame (mm)
    float rx=0, ry=0, rz=0;            // tilting angles (degrees)

    // Precomputed rotation matrix elements.
    struct Matrix {
        float r00=1, r01=0, r10=0, r11=1, r20=0, r21=0;
        float tx=0, ty=0, tz=0;
    };

    // Force matrix precomputation (idempotent).
    void prepare() const {
        if (prepared_) return;
        const float DEG = 3.14159265f / 180.f;
        float cx=std::cos(rx*DEG), sx=std::sin(rx*DEG);
        float cy=std::cos(ry*DEG), sy=std::sin(ry*DEG);
        float cz=std::cos(rz*DEG), sz=std::sin(rz*DEG);
        mat_.r00 =  cy*cz;              mat_.r01 = -cy*sz;
        mat_.r10 =  sx*sy*cz + cx*sz;   mat_.r11 = -sx*sy*sz + cx*cz;
        mat_.r20 = -cx*sy*cz + sx*sz;   mat_.r21 =  cx*sy*sz + sx*cz;
        mat_.tx = x;  mat_.ty = y;  mat_.tz = z;
        prepared_ = true;
    }

    // Transform a point from detector plane to lab frame.
    void toLab(float dx, float dy, float &lx, float &ly, float &lz) const {
        prepare();
        lx = mat_.r00*dx + mat_.r01*dy + mat_.tx;
        ly = mat_.r10*dx + mat_.r11*dy + mat_.ty;
        lz = mat_.r20*dx + mat_.r21*dy + mat_.tz;
    }

    // Rotation only (no translation). For drawing in detector-local space.
    void rotate(float dx, float dy, float &ox, float &oy) const {
        prepare();
        ox = mat_.r00*dx + mat_.r01*dy;
        oy = mat_.r10*dx + mat_.r11*dy;
    }

    // Access the cached matrix directly (auto-prepares).
    const Matrix& matrix() const { prepare(); return mat_; }

private:
    mutable Matrix mat_;
    mutable bool   prepared_ = false;
};
