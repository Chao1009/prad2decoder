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

    // Precomputed rotation matrix elements (full 3x3, row-major: rIJ).
    struct Matrix {
        float r00=1, r01=0, r02=0;
        float r10=0, r11=1, r12=0;
        float r20=0, r21=0, r22=1;
        float tx=0, ty=0, tz=0;
    };

    // Force matrix precomputation (idempotent).
    void prepare() const {
        if (prepared_) return;
        const float DEG = 3.14159265f / 180.f;
        float cx=std::cos(rx*DEG), sx=std::sin(rx*DEG);
        float cy=std::cos(ry*DEG), sy=std::sin(ry*DEG);
        float cz=std::cos(rz*DEG), sz=std::sin(rz*DEG);
        // R = Rx * Ry * Rz (Euler XYZ, intrinsic).
        mat_.r00 =  cy*cz;              mat_.r01 = -cy*sz;              mat_.r02 =  sy;
        mat_.r10 =  sx*sy*cz + cx*sz;   mat_.r11 = -sx*sy*sz + cx*cz;   mat_.r12 = -sx*cy;
        mat_.r20 = -cx*sy*cz + sx*sz;   mat_.r21 =  cx*sy*sz + sx*cz;   mat_.r22 =  cx*cy;
        mat_.tx = x;  mat_.ty = y;  mat_.tz = z;
        prepared_ = true;
    }

    // Transform a point from detector plane to lab frame.
    // Detector plane has z_local=0; full 3D toLab takes a local 3D point.
    void toLab(float dx, float dy, float &lx, float &ly, float &lz) const {
        prepare();
        lx = mat_.r00*dx + mat_.r01*dy + mat_.tx;
        ly = mat_.r10*dx + mat_.r11*dy + mat_.ty;
        lz = mat_.r20*dx + mat_.r21*dy + mat_.tz;
    }

    // Inverse: lab → detector-local. R is orthonormal so R^{-1} = R^T.
    void labToLocal(float lx, float ly, float lz,
                    float &dx, float &dy, float &dz) const {
        prepare();
        float ux = lx - mat_.tx, uy = ly - mat_.ty, uz = lz - mat_.tz;
        dx = mat_.r00*ux + mat_.r10*uy + mat_.r20*uz;
        dy = mat_.r01*ux + mat_.r11*uy + mat_.r21*uz;
        dz = mat_.r02*ux + mat_.r12*uy + mat_.r22*uz;
    }

    // Rotation only (no translation). For drawing in detector-local space.
    void rotate(float dx, float dy, float &ox, float &oy) const {
        prepare();
        ox = mat_.r00*dx + mat_.r01*dy;
        oy = mat_.r10*dx + mat_.r11*dy;
    }

    // The detector's surface normal in the lab frame (third column of R).
    void normal(float &nx, float &ny, float &nz) const {
        prepare();
        nx = mat_.r02;  ny = mat_.r12;  nz = mat_.r22;
    }

    // Access the cached matrix directly (auto-prepares).
    const Matrix& matrix() const { prepare(); return mat_; }

private:
    mutable Matrix mat_;
    mutable bool   prepared_ = false;
};
