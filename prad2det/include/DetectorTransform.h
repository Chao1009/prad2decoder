#pragma once
//=============================================================================
// DetectorTransform.h — planar detector coordinate transform
//
// Transforms detector-plane coordinates (x, y) to lab frame via
// Euler rotation (Rx * Ry * Rz) then translation.
// Reusable for HyCal, GEMs, or any planar detector.
//=============================================================================

#include <cmath>

struct DetectorTransform {
    float x=0, y=0, z=0;               // detector origin in lab frame (mm)
    float rx=0, ry=0, rz=0;            // tilting angles (degrees)

    // Transform a point from detector plane to lab frame.
    void toLab(float dx, float dy, float &lx, float &ly, float &lz) const {
        const float DEG = 3.14159265f / 180.f;
        float cx=std::cos(rx*DEG), sx=std::sin(rx*DEG);
        float cy=std::cos(ry*DEG), sy=std::sin(ry*DEG);
        float cz=std::cos(rz*DEG), sz=std::sin(rz*DEG);
        // R = Rx * Ry * Rz applied to (dx, dy, 0)
        float px =  cy*cz*dx - cy*sz*dy;
        float py = (sx*sy*cz + cx*sz)*dx + (-sx*sy*sz + cx*cz)*dy;
        float pz = (-cx*sy*cz + sx*sz)*dx + (cx*sy*sz + sx*cz)*dy;
        lx = px + x;
        ly = py + y;
        lz = pz + z;
    }

    // Rotation only (no translation). For drawing in detector-local space.
    void rotate(float dx, float dy, float &ox, float &oy) const {
        const float DEG = 3.14159265f / 180.f;
        float cx=std::cos(rx*DEG), sx=std::sin(rx*DEG);
        float cy=std::cos(ry*DEG), sy=std::sin(ry*DEG);
        float cz=std::cos(rz*DEG), sz=std::sin(rz*DEG);
        ox =  cy*cz*dx - cy*sz*dy;
        oy = (sx*sy*cz + cx*sz)*dx + (-sx*sy*sz + cx*cz)*dy;
    }
};
