// test/et_feeder.cpp — Feed an evio file to an ET system event-by-event
//
// Usage: et_feeder <evio_file> [-h host] [-p port] [-f et_file] [-i interval_ms]

#include "EtConfigWrapper.h"
#include "EvChannel.h"
#include <csignal>
#include <thread>
#include <chrono>
#include <iostream>
#include <cstring>
#include <cstdlib>
#include <unistd.h>

#define PROGRESS_COUNT 100

using namespace std::chrono;

volatile std::sig_atomic_t gSignalStatus;

void signal_handler(int signal) { gSignalStatus = signal; }

static void usage(const char *prog) {
    std::cerr << "Usage: " << prog << " <evio_file> [-h host] [-p port] [-f et_file] [-i interval_ms]\n";
}

int main(int argc, char* argv[])
{
    std::string host = "localhost";
    int port = 11111;
    std::string et_file = "/tmp/et_feeder";
    int interval = 100;

    int opt;
    while ((opt = getopt(argc, argv, "h:p:f:i:")) != -1) {
        switch (opt) {
        case 'h': host = optarg; break;
        case 'p': port = std::atoi(optarg); break;
        case 'f': et_file = optarg; break;
        case 'i': interval = std::atoi(optarg); break;
        default:  usage(argv[0]); return 1;
        }
    }
    if (optind >= argc) { usage(argv[0]); return 1; }
    std::string evio_file = argv[optind];

    et_sys_id et_id;
    et_att_id att_id;

    // open ET system
    et_wrap::OpenConfig conf;
    conf.set_cast(ET_DIRECT);
    conf.set_host(host.c_str());
    conf.set_serverport(port);

    std::vector<char> fname(et_file.begin(), et_file.end());
    fname.push_back('\0');
    auto status = et_open(&et_id, fname.data(), conf.configure().get());

    if (status != ET_OK) {
        std::cerr << "Cannot open ET at " << host << ":" << port << " with " << et_file << "\n";
        return -1;
    }

    // attach to GRAND CENTRAL
    status = et_station_attach(et_id, ET_GRANDCENTRAL, &att_id);
    if (status != ET_OK) {
        std::cerr << "Failed to attach to the ET Grand Central Station.\n";
        return -1;
    }

    // evio file reader
    evc::EvChannel chan;
    if (chan.Open(evio_file) != evc::status::success) {
        std::cerr << "Failed to open coda file \"" << evio_file << "\"\n";
        return -1;
    }

    // install signal handler
    std::signal(SIGINT, signal_handler);
    int count = 0;
    et_event *ev;
    while ((chan.Read() == evc::status::success) && et_alive(et_id)) {
        if (gSignalStatus == SIGINT) {
            std::cout << "Received control-C, exiting...\n";
            break;
        }
        system_clock::time_point start(system_clock::now());
        system_clock::time_point next(start + std::chrono::milliseconds(interval));

        if (++count % PROGRESS_COUNT == 0) {
            std::cout << "Read and fed " << count << " events to ET.\r" << std::flush;
        }

        uint32_t *buf = chan.GetRawBuffer();
        size_t nbytes = (buf[0] + 1) * sizeof(uint32_t);

        status = et_event_new(et_id, att_id, &ev, ET_SLEEP, nullptr, nbytes);
        if (status != ET_OK) {
            std::cerr << "Failed to add new event to the ET system.\n";
            return -1;
        }
        void *data;
        et_event_getdata(ev, &data);
        memcpy(data, buf, nbytes);
        et_event_setlength(ev, nbytes);

        status = et_event_put(et_id, att_id, ev);
        if (status != ET_OK) {
            std::cerr << "Failed to put event back to the ET system.\n";
            return -1;
        }

        std::this_thread::sleep_until(next);
    }
    std::cout << "Read and fed " << count << " events to ET\n";

    chan.Close();
    return 0;
}
