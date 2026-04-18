# Source this file to set up the PRad2 environment (csh/tcsh).
#   source <prefix>/bin/setup.csh
#
# csh has no portable equivalent of bash's BASH_SOURCE.  We try, in order:
#   1. $_ — tcsh sets this to the source command's tokens
#   2. $0 — works under some csh variants when sourced
#   3. $PRAD2_DIR already set by the user
# If none work, the user must `setenv PRAD2_DIR <prefix>` before sourcing.

set _src = ( $_ )
if ( $#_src > 1 ) then
    set _setup_path = "$_src[2]"
else if ( $?0 && "$0" != "" && "$0" != "csh" && "$0" != "tcsh" && "$0" != "-csh" && "$0" != "-tcsh" ) then
    set _setup_path = "$0"
else
    set _setup_path = ""
endif
unset _src

if ( "$_setup_path" != "" ) then
    set _setup_dir = `dirname "$_setup_path"`
    setenv PRAD2_DIR `cd "$_setup_dir/.." && pwd`
    unset _setup_dir
else if ( ! $?PRAD2_DIR ) then
    echo "setup.csh: cannot determine script path; please 'setenv PRAD2_DIR <prefix>' first" >&2
endif
unset _setup_path

if ( $?PRAD2_DIR ) then
    if ( $?PATH ) then
        setenv PATH "${PRAD2_DIR}/bin:${PATH}"
    else
        setenv PATH "${PRAD2_DIR}/bin"
    endif
    if ( $?LD_LIBRARY_PATH ) then
        setenv LD_LIBRARY_PATH "${PRAD2_DIR}/lib:${LD_LIBRARY_PATH}"
    else
        setenv LD_LIBRARY_PATH "${PRAD2_DIR}/lib"
    endif
    if ( $?PYTHONPATH ) then
        setenv PYTHONPATH "${PRAD2_DIR}/lib/prad2py:${PYTHONPATH}"
    else
        setenv PYTHONPATH "${PRAD2_DIR}/lib/prad2py"
    endif
    setenv PRAD2_DATABASE_DIR "${PRAD2_DIR}/share/prad2evviewer/database"
    setenv PRAD2_RESOURCE_DIR "${PRAD2_DIR}/share/prad2evviewer/resources"
endif
