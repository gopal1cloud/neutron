::cisco::eem::event_register_syslog occurs 1 pattern $_syslog_pattern maxrun 90
#------------------------------------------------------------------
# USAGE:
#  
#  copy .tcl file to flash:
#  using HSRP statechange syslog as example:
#   event manager environment _syslog_pattern .*STATECHANGE.*Port-channel.*
#   event manager directory user policy "flash:/"
#   event manager policy test.tcl type user
#
# EEM policy to monitor for a specified syslog message.
# Designed to be used for syslog interface-down messages.  
# When event is triggered, the given config commands will be run.
#
# July 2005, Cisco EEM team
#
# Copyright (c) 2005-2006 by cisco Systems, Inc.
# All rights reserved.
#------------------------------------------------------------------
### The following EEM environment variables are used:
###
### _syslog_pattern (mandatory)        - A regular expression pattern match string 
###                                      that is used to compare syslog messages
###                                      to determine when policy runs 
### Example: _syslog_pattern             .*UPDOWN.*FastEthernet0/0.* 
###
### _email_server (mandatory)          - A Simple Mail Transfer Protocol (SMTP)
###                                      mail server used to send e-mail.
### Example: _email_server               mailserver.customer.com
###
### _email_from (mandatory)            - The address from which e-mail is sent.
### Example: _email_from                 devtest@customer.com
###       
### _email_to (mandatory)              - The address to which e-mail is sent.
### Example: _email_to                   engineering@customer.com
###       
### _email_cc (optional)               - The address to which the e-mail must
###                                      be copied.
### Example: _email_cc                   manager@customer.com
###       
### _config_cmd1 (optional)            - The first configuration command that
###                                      is executed.
### Example: _config_cmd1                interface Ethernet1/0 
###       
### _config_cmd2 (optional)            - The second configuration command that
###                                      is executed.
### Example: _config_cmd2                no shutdown
###       
# check if all the env variables we need exist
# If any of them doesn't exist, print out an error msg and quit
#if {![info exists _email_server]} {
#    set result \
#        "Policy cannot be run: variable _email_server has not been set"
#    error $result $errorInfo
#}         

namespace import ::cisco::eem::*
namespace import ::cisco::lib::*
# 1. query the information of latest triggered eem event
array set arr_einfo [event_reqinfo]
if {$_cerrno != 0} {
    set result [format "component=%s; subsys err=%s; posix err=%s;\n%s" \
        $_cerr_sub_num $_cerr_sub_err $_cerr_posix_err $_cerr_str]
    error $result 
}         
set msg $arr_einfo(msg)
set config_cmds ""
# 2. execute the user-defined config commands
if [catch {cli_open} result] {
    error $result $errorInfo
} else {  
    array set cli1 $result
} 

cli_write $cli1(fd) "ssh -l root 10.1.10.188"
cli_read_pattern $cli1(fd) "Password"
cli_write $cli1(fd) "password\r"
cli_read_pattern $cli1(fd) "root@localhost"   
cli_write $cli1(fd) "python rpc_tester.py --config-file /etc/neutron/neutron.conf\r" 

#if [catch {cli_exec $cli1(fd) "ssh -l root 10.1.10.188"} result] {
#    error $result $errorInfo
#}  

#if [catch {cli_exec $cli1(fd) "en"} result] {
#    error $result $errorInfo
#}         
#if [catch {cli_exec $cli1(fd) "config t"} result] {
#    error $result $errorInfo
#}         
#if {[info exists _config_cmd1]} {
#    if [catch {cli_exec $cli1(fd) $_config_cmd1} result] {
#        error $result $errorInfo
#    }     
#    append config_cmds $_config_cmd1
#}         
#if {[info exists _config_cmd2]} {
#    if [catch {cli_exec $cli1(fd) $_config_cmd2} result] {
#        error $result $errorInfo
#    }     
#    append config_cmds "\n"
#    append config_cmds $_config_cmd2
#}         
#if [catch {cli_exec $cli1(fd) "end"} result] {
#    error $result $errorInfo
#}         

after 5000
if [catch {cli_close $cli1(fd) $cli1(tty_id)} result] {
    error $result $errorInfo
}
         
#after 60000
# 3. send the notification email
#set routername [info hostname]
#if {[string match "" $routername]} {
#    error "Host name is not configured"
#}         
#if [catch {smtp_subst [file join $tcl_library email_template_cfg.tm]} result] {
#    error $result $errorInfo
#}         
#if [catch {smtp_send_email $result} result] {
#    error $result $errorInfo
#}         
          
