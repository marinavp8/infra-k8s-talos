#!/usr/bin/env python3
"""Hub L2 sobre UDP para QEMU (-netdev dgram): reenvía cada trama al resto de puertos de VM.
Uso: l2hub.py <hub_port> <vm_port> [<vm_port> ...]"""
import socket, sys

hub = int(sys.argv[1])
vm_ports = [int(p) for p in sys.argv[2:]]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", hub))
while True:
    data, (_, src) = s.recvfrom(65535)
    for p in vm_ports:
        if p != src:
            s.sendto(data, ("127.0.0.1", p))
