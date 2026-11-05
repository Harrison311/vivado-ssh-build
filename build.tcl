# Server batch entry. No simulation or source/config digest generation.
set result_root [file join [file dirname [info script]] results]
proc phase {name} {
    global result_root
    set f [open [file join $result_root phase_events.tsv] a]
    puts $f "[clock milliseconds]\t$name"
    close $f
    puts "BUILD_PHASE: $name"
    flush stdout
}
set code [catch {
    phase Preparing
    source [file join [file dirname [info script]] settings.tcl]
    create_project build [file join [pwd] project] -part $part
    set_property target_language Verilog [current_project]
    foreach script $ip_scripts { source $script }
    add_files -norecurse $rtl_files
    if {[llength $include_dirs]} {
        set_property include_dirs $include_dirs [get_filesets sources_1]
    }
    if {[llength $xdc_files]} {
        add_files -norecurse -fileset constrs_1 $xdc_files
        set_property PROCESSING_ORDER LATE [get_files $xdc_files]
    }
    set_property top $top [get_filesets sources_1]
    update_compile_order -fileset sources_1
    foreach ip [get_ips -quiet] {
        if {[lsearch -exact [get_property DELIVERED_TARGETS $ip] synthesis] < 0 ||
            [lsearch -exact [get_property STALE_TARGETS $ip] synthesis] >= 0} {
            generate_target synthesis $ip
        }
    }
    phase Synthesis
    launch_runs synth_1 -jobs $jobs
    wait_on_run synth_1
    if {[get_property PROGRESS [get_runs synth_1]] ne "100%" ||
        ![string match *Complete* [get_property STATUS [get_runs synth_1]]]} {
        error "Synthesis failed: [get_property STATUS [get_runs synth_1]]"
    }
    phase Implementation
    launch_runs impl_1 -to_step route_design -jobs $jobs
    wait_on_run impl_1
    if {[get_property PROGRESS [get_runs impl_1]] ne "100%" ||
        ![string match *Complete* [get_property STATUS [get_runs impl_1]]]} {
        error "Implementation failed: [get_property STATUS [get_runs impl_1]]"
    }
    phase Checks
    open_run impl_1
    set timing_report [report_timing_summary -report_unconstrained -return_string]
    set f [open [file join $result_root timing_summary.rpt] w]
    puts $f $timing_report
    close $f
    report_utilization -file [file join $result_root utilization.rpt]
    report_drc -file [file join $result_root drc.rpt]
    set checks [check_timing -override_defaults {no_clock unconstrained_internal_endpoints} -return_string]
    set f [open [file join $result_root check_timing.rpt] w]
    puts $f $checks
    close $f
    if {![string match {*All user specified timing constraints are met.*} $timing_report]} {
        error "Timing summary does not confirm all timing constraints met; review timing_summary.rpt"
    }
    if {![regexp {checking no_clock \(0\)} $checks] ||
        ![regexp {checking unconstrained_internal_endpoints \(0\)} $checks]} {
        error "Unconstrained timing checks nonzero or unrecognized; review check_timing.rpt"
    }
    foreach delay {max min} {
        set paths [get_timing_paths -delay_type $delay -max_paths 1]
        if {[llength $paths] == 0 || [get_property SLACK $paths] < 0} {
            error "Missing or failing $delay timing paths"
        }
    }
    set violations [get_drc_violations -quiet -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]
    if {[llength $violations]} { error "Blocking DRC violations: $violations" }
    phase {Generate Bitstream}
    write_bitstream -force [file join $result_root design.bit]
    if {[llength [get_debug_cores -quiet]]} {
        phase {Debug Probes}
        write_debug_probes -force [file join $result_root design.ltx]
    }
    phase Complete
} message options]
set f [open [file join $result_root build_status.txt] w]
if {$code} {
    puts $f FAIL
    close $f
    set f [open [file join $result_root failure.txt] w]
    puts $f $message
    if {[dict exists $options -errorinfo]} { puts $f [dict get $options -errorinfo] }
    close $f
    puts stderr $message
    exit 1
}
puts $f PASS
close $f
exit 0
