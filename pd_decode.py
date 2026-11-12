"""USB Power Delivery message decoder (header, PDOs, RDOs, VDM summary)."""
import struct

CONTROL = {
    1: "GoodCRC", 2: "GotoMin", 3: "Accept", 4: "Reject", 5: "Ping", 6: "PS_RDY",
    7: "Get_Source_Cap", 8: "Get_Sink_Cap", 9: "DR_Swap", 10: "PR_Swap", 11: "VCONN_Swap",
    12: "Wait", 13: "Soft_Reset", 14: "Data_Reset", 15: "Data_Reset_Complete",
    16: "Not_Supported", 17: "Get_Source_Cap_Extended", 18: "Get_Status", 19: "FR_Swap",
    20: "Get_PPS_Status", 21: "Get_Country_Codes", 22: "Get_Sink_Cap_Extended",
    23: "Get_Source_Info", 24: "Get_Revision",
}
DATA = {
    1: "Source_Capabilities", 2: "Request", 3: "BIST", 4: "Sink_Capabilities",
    5: "Battery_Status", 6: "Alert", 7: "Get_Country_Info", 8: "Enter_USB",
    9: "EPR_Request", 10: "EPR_Mode", 11: "Source_Info", 12: "Revision", 15: "Vendor_Defined",
}
EXTENDED = {
    1: "Source_Capabilities_Extended", 2: "Status", 3: "Get_Battery_Cap", 4: "Get_Battery_Status",
    5: "Battery_Capabilities", 6: "Get_Manufacturer_Info", 7: "Manufacturer_Info",
    8: "Security_Request", 9: "Security_Response", 10: "Firmware_Update_Request",
    11: "Firmware_Update_Response", 12: "PPS_Status", 13: "Country_Info", 14: "Country_Codes",
    15: "Sink_Capabilities_Extended", 16: "Extended_Control", 17: "EPR_Source_Capabilities",
    18: "EPR_Sink_Capabilities", 30: "Vendor_Defined_Extended",
}
VDM_CMDS = {1: "Discover Identity", 2: "Discover SVIDs", 3: "Discover Modes", 4: "Enter Mode",
            5: "Exit Mode", 6: "Attention"}
VDM_TYPES = {0: "REQ", 1: "ACK", 2: "NAK", 3: "BUSY"}
SOP_NAMES = {0: "SOP", 1: "SOP'", 2: "SOP''"}


def _bits(v, hi, lo):
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def decode_pdo(pdo, sink=False):
    """Returns (kind, short text, detail dict)."""
    t = _bits(pdo, 31, 30)
    if t == 0:
        v, i = _bits(pdo, 19, 10) * 0.05, _bits(pdo, 9, 0) * 0.01
        flags = [name for bit, name in ((29, "DRP"), (28, "Suspend") if not sink else (28, "HighCap"),
                                        (27, "Unconstrained"), (26, "USB Comm"), (25, "DRD"),
                                        (24, "Unchunked") if not sink else (24, ""),
                                        (23, "EPR") if not sink else (23, ""))
                 if name and pdo >> bit & 1]
        return "Fixed", f"{v:g}V {i:.2f}A ({v * i:.1f}W)", {"V": v, "A": i, "flags": flags}
    if t == 1:
        vmax, vmin, w = _bits(pdo, 29, 20) * 0.05, _bits(pdo, 19, 10) * 0.05, _bits(pdo, 9, 0) * 0.25
        return "Battery", f"{vmin:g}-{vmax:g}V {w:g}W", {"Vmin": vmin, "Vmax": vmax, "W": w}
    if t == 2:
        vmax, vmin, i = _bits(pdo, 29, 20) * 0.05, _bits(pdo, 19, 10) * 0.05, _bits(pdo, 9, 0) * 0.01
        return "Variable", f"{vmin:g}-{vmax:g}V {i:.2f}A", {"Vmin": vmin, "Vmax": vmax, "A": i}
    sub = _bits(pdo, 29, 28)
    if sub == 0:
        vmax, vmin, i = _bits(pdo, 24, 17) * 0.1, _bits(pdo, 15, 8) * 0.1, _bits(pdo, 6, 0) * 0.05
        return "PPS", f"{vmin:g}-{vmax:g}V {i:.2f}A", {"Vmin": vmin, "Vmax": vmax, "A": i,
                                                       "flags": ["PPS Power Limited"] if pdo >> 27 & 1 else []}
    if sub == 1:
        vmax, vmin, w = _bits(pdo, 25, 17) * 0.1, _bits(pdo, 15, 8) * 0.1, _bits(pdo, 7, 0)
        return "EPR AVS", f"{vmin:g}-{vmax:g}V {w}W", {"Vmin": vmin, "Vmax": vmax, "W": w}
    if sub == 2:
        i15, i20 = _bits(pdo, 19, 10) * 0.01, _bits(pdo, 9, 0) * 0.01
        return "SPR AVS", f"15V {i15:.2f}A / 20V {i20:.2f}A", {"A@15V": i15, "A@20V": i20}
    return "APDO?", f"0x{pdo:08X}", {}


def decode_rdo(rdo, src_pdos):
    pos = _bits(rdo, 31, 28)
    kind = src_pdos[pos - 1][0] if 0 < pos <= len(src_pdos) else None
    flags = [name for bit, name in ((27, "GiveBack"), (26, "CapMismatch"), (25, "USB Comm"),
                                    (24, "NoSuspend"), (23, "Unchunked"), (22, "EPR"))
             if rdo >> bit & 1]
    if kind == "PPS" or kind in ("EPR AVS", "SPR AVS"):
        v = _bits(rdo, 20, 9) * (0.02 if kind == "PPS" else 0.025)
        i = _bits(rdo, 6, 0) * 0.05
        text = f"PDO#{pos} {kind} {v:.2f}V {i:.2f}A"
    else:
        op, mx = _bits(rdo, 19, 10) * 0.01, _bits(rdo, 9, 0) * 0.01
        target = f" ({src_pdos[pos - 1][1].split(' (')[0]})" if kind else ""
        text = f"PDO#{pos}{target} Op {op:.2f}A Max {mx:.2f}A"
    return text, flags


class PdDecoder:
    """Stateful decoder: remembers the latest Source_Capabilities to interpret Requests."""

    def __init__(self):
        self.src_pdos = []

    def reset(self):
        self.src_pdos = []

    def decode(self, wire: bytes, sop: int = 0) -> dict:
        if len(wire) < 2:
            return {"name": "Invalid", "summary": wire.hex(), "details": []}
        hdr = struct.unpack_from("<H", wire, 0)[0]
        mtype, n_obj, ext = hdr & 0x1F, _bits(hdr, 14, 12), hdr >> 15 & 1
        rev = {0: "1.0", 1: "2.0", 2: "3.x"}.get(_bits(hdr, 7, 6), "?")
        if sop == 0:
            roles = f"{'Source' if hdr >> 8 & 1 else 'Sink'}/{'DFP' if hdr >> 5 & 1 else 'UFP'}"
        else:
            roles = "Cable" if hdr >> 8 & 1 else "Port"
        objs = [struct.unpack_from("<I", wire, 2 + 4 * k)[0]
                for k in range(n_obj) if 2 + 4 * k + 4 <= len(wire)]
        if ext:
            name = EXTENDED.get(mtype, f"Extended_{mtype}")
        elif n_obj == 0:
            name = CONTROL.get(mtype, f"Control_{mtype}")
        else:
            name = DATA.get(mtype, f"Data_{mtype}")

        summary, details = "", []
        if not ext and name in ("Source_Capabilities", "Sink_Capabilities"):
            sink = name == "Sink_Capabilities"
            pdos = [decode_pdo(o, sink) for o in objs]
            if not sink:
                self.src_pdos = pdos
            summary = ", ".join(p[1].split(" (")[0] for p in pdos)
            details = [{"label": f"PDO#{k + 1} {p[0]}", "value": p[1],
                        "flags": p[2].get("flags", []), "raw": f"0x{objs[k]:08X}"}
                       for k, p in enumerate(pdos)]
        elif not ext and name in ("Request", "EPR_Request") and objs:
            text, flags = decode_rdo(objs[0], self.src_pdos)
            summary = text
            details = [{"label": "RDO", "value": text, "flags": flags, "raw": f"0x{objs[0]:08X}"}]
            if name == "EPR_Request" and len(objs) > 1:
                kind, t, _ = decode_pdo(objs[1])
                details.append({"label": f"PDO copy {kind}", "value": t, "flags": [], "raw": f"0x{objs[1]:08X}"})
        elif not ext and name == "Vendor_Defined" and objs:
            h = objs[0]
            svid = h >> 16
            if h >> 15 & 1:
                cmd = VDM_CMDS.get(h & 0x1F, f"Cmd {h & 0x1F}")
                summary = f"SVID 0x{svid:04X} {cmd} {VDM_TYPES[_bits(h, 7, 6)]}"
            else:
                summary = f"Unstructured SVID 0x{svid:04X}"
            details = [{"label": f"VDO#{k}", "value": "", "flags": [], "raw": f"0x{o:08X}"}
                       for k, o in enumerate(objs)]
        elif objs:
            details = [{"label": f"Obj#{k + 1}", "value": "", "flags": [], "raw": f"0x{o:08X}"}
                       for k, o in enumerate(objs)]

        return {
            "name": name, "summary": summary, "details": details,
            "sop": SOP_NAMES.get(sop, f"SOP#{sop}"), "roles": roles, "rev": rev,
            "msg_id": _bits(hdr, 11, 9), "n_obj": n_obj, "header": f"0x{hdr:04X}",
            "raw": wire.hex(" ").upper(),
        }
