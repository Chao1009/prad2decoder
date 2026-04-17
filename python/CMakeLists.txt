# python/CMakeLists.txt — prad2py (pybind11 bindings for prad2dec)
#
# Built when BUILD_PYTHON=ON (root CMakeLists).  Fetches pybind11 via
# FetchContent so no system install is required.  The resulting
# extension module lands in ${CMAKE_BINARY_DIR}/python/ — add that
# directory to PYTHONPATH to import it, e.g.:
#
#   export PYTHONPATH=$PWD/build/python:$PYTHONPATH
#   python -c "import prad2py; print(prad2py.__version__)"

include(FetchContent)

FetchContent_Declare(pybind11
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG        v2.13.6
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(pybind11)

pybind11_add_module(prad2py MODULE prad2py.cpp)

target_link_libraries(prad2py PRIVATE
    prad2dec
    nlohmann_json::nlohmann_json
)

target_compile_definitions(prad2py PRIVATE
    DATABASE_DIR="${DATABASE_DIR}"
)

set_target_properties(prad2py PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python"
)

# Install alongside a tiny site-layout so `pip install --editable` users
# can also find it, but the main flow is the PYTHONPATH export above.
include(GNUInstallDirs)
install(TARGETS prad2py
    LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}/prad2py"
)

message(STATUS "Python bindings: output -> ${CMAKE_BINARY_DIR}/python/")
