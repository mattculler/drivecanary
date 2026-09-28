# smartctl -j fixtures

Real `smartctl --json` captures, taken from the test data of Scrutiny
(https://github.com/AnalogJ/scrutiny, `webapp/backend/pkg/models/testdata/`, MIT licence, copyright
Jason Kulatunga) during the 2026-09-28 audit. They cover the device types and failure shapes the parser
must handle; nothing in them is from this LAN.

| file | what |
|---|---|
| smart-ata.json | WDC WD140EDFZ, SATA HDD via `sat`, healthy (7.0) |
| smart-ata-full.json | Samsung 860 EVO, SATA SSD, `-x` output with every log (7.0) |
| smart-sat.json | a SAT device with no `smart_status` and no `local_time` (7.0) |
| smart-scsi.json | SEAGATE ST4000NM0043, SAS, `scsi_grown_defect_list` (no smartctl version block) |
| smart-nvme.json | INTEL SSDPEKNW010T8, NVMe, healthy (7.1) |
| smart-nvme-failed.json | Samsung 970 EVO, NVMe, `passed: true` with 7 media errors (7.0) |
| smart-fail2.json | Hitachi HDS721050DLE630 over a JMicron USB bridge, FAILED, exit 216 = bits 3,4,6,7 (7.1) |
| smart-fail.json | open failed: no identity, exit 2, one error message (7.0) |
