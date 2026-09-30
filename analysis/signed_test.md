# Signed-return per-day IC test

Run: 2026-09-29 00:46  ·  script: scripts/research/signed_test.py  ·  pre-registration: analysis/signed_test_preregistration.md (commit daf7354 in the private development repository)

## 1. Sample

- CSV: 1,289,872 rows, 362 days (byte count matches 137,121,407).
- Matched (`map_status == ok`): 964,504 rows, 288 days, 2024-11-01 → 2025-12-26.
- **Primary sample (sign = sign(DLYRETX)):** 952,002 rows, 288 days; dropped 12,502 rows with DLYRETX = 0.
- Robustness sample (sign = sign(DLYRET)): 952,045 rows; dropped 12,459 rows with DLYRET = 0.
- Direction mix, primary sample: long (DLYRETX > 0) 51.7%, short 48.3%.
- Stocks per day: min 2,947, median 3,296.

Signed forward return `sfwd = sign(day-t return) × fwd_return_3d`. Net = sfwd − 120 bps (long) or − 150 bps (short). NW t: Bartlett kernel, 3 lags decide, 5 lags for reference.

## 2. Primary test (confirmatory)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC vs signed fwd (PRIMARY) | 288 | -0.0041 | -0.0038 | 0.0628 | 48% | -1.11 | -1.18 | -1.21 |
| composite IC vs raw fwd, same rows (control) | 288 | -0.0101 | -0.0115 | 0.0759 | 43% | -2.26 | -1.67 | -1.64 |

Top composite decile, within day, direction-appropriate costs (bps):

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| top-decile gross signed return | 288 | -3.1 | +8.0 | 148.4 | 54% | -0.35 | -0.39 | -0.39 |
| top-decile NET signed return (decision) | 288 | -135.7 | -126.8 | 148.9 | 12% | -15.46 | -16.88 | -16.74 |

Decision rule: alive iff primary NW3 t ≥ +2.0 and mean daily top-decile net > 0; reversal iff t ≤ −2.0; else dead.
- Primary NW3 t = -1.18; mean daily top-decile net = -135.7 bps.
- **Verdict: dead**

Robustness, sign(DLYRET) (no decision weight):

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC vs signed fwd, sign(DLYRET) | 288 | -0.0040 | -0.0030 | 0.0628 | 48% | -1.09 | -1.16 | -1.20 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| top-decile NET signed return, sign(DLYRET) | 288 | -135.6 | -125.7 | 149.0 | 12% | -15.45 | -16.84 | -16.69 |
- Verdict under the same rule with sign(DLYRET): dead

Autocorrelation of the daily primary IC series (lags 1–5): -0.048, -0.016, -0.043, -0.013, -0.017

## 3. EXPLORATORY — per-day IC by score, signed vs raw (same rows)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite — signed | 288 | -0.0041 | -0.0038 | 0.0628 | 48% | -1.11 | -1.18 | -1.21 |
| composite — raw | 288 | -0.0101 | -0.0115 | 0.0759 | 43% | -2.26 | -1.67 | -1.64 |
| volume — signed | 288 | -0.0031 | -0.0050 | 0.0568 | 46% | -0.94 | -0.92 | -0.94 |
| volume — raw | 288 | -0.0020 | -0.0020 | 0.0695 | 49% | -0.50 | -0.35 | -0.34 |
| return_mag — signed | 288 | -0.0051 | -0.0091 | 0.0997 | 46% | -0.86 | -0.98 | -1.02 |
| return_mag — raw | 288 | -0.0089 | -0.0132 | 0.0740 | 46% | -2.05 | -2.01 | -2.08 |
| sector_rs — signed | 288 | +0.0008 | +0.0019 | 0.0437 | 51% | +0.32 | +0.26 | +0.26 |
| sector_rs — raw | 288 | -0.0006 | +0.0021 | 0.0422 | 53% | -0.22 | -0.17 | -0.16 |
| delay — signed | 166 | -0.0014 | +0.0039 | 0.0986 | 52% | -0.19 | -0.18 | -0.18 |
| delay — raw | 166 | -0.0320 | -0.0421 | 0.1096 | 36% | -3.77 | -2.49 | -2.43 |

## 4. EXPLORATORY — within-day decile signed spreads (bps)

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite: D10 − D1 | 288 | -2.6 | +3.4 | 106.7 | 52% | -0.41 | -0.39 | -0.38 |
| composite: D10 − rest | 288 | +1.4 | +6.6 | 82.1 | 56% | +0.29 | +0.26 | +0.25 |
| volume: D10 − D1 | 288 | +1.7 | -0.1 | 105.8 | 50% | +0.27 | +0.25 | +0.25 |
| volume: D10 − rest | 288 | +0.5 | +0.9 | 75.6 | 51% | +0.12 | +0.10 | +0.10 |
| return_mag: D10 − D1 | 288 | -9.7 | -8.0 | 141.1 | 46% | -1.17 | -1.33 | -1.36 |
| return_mag: D10 − rest | 288 | -3.5 | -1.8 | 83.2 | 49% | -0.71 | -0.75 | -0.72 |
| sector_rs: D10 − D1 | 288 | +7.0 | +9.0 | 83.7 | 55% | +1.43 | +1.16 | +1.16 |
| sector_rs: D10 − rest | 288 | +4.8 | +2.9 | 49.6 | 53% | +1.65 | +1.42 | +1.48 |
| delay: D10 − D1 | 166 | +2.9 | +6.6 | 109.3 | 52% | +0.34 | +0.38 | +0.37 |
| delay: D10 − rest | 166 | +4.8 | +4.8 | 69.2 | 57% | +0.89 | +0.95 | +0.97 |
| composite: D10 − D1, RAW (control) | 288 | -3.8 | -4.2 | 118.6 | 46% | -0.55 | -0.40 | -0.39 |
| composite: D10 − rest, RAW (control) | 288 | +0.8 | +2.3 | 79.4 | 51% | +0.17 | +0.13 | +0.12 |

Composite, mean signed return by within-day decile (time-series average of daily decile means, bps): D1 -0.5, D2 -7.7, D3 -4.0, D4 -3.4, D5 -4.1, D6 -5.9, D7 -2.5, D8 -6.3, D9 -6.0, D10 -3.1

## 5. EXPLORATORY — top composite decile by trade direction (bps)

Top-decile direction mix: long 57.9%, short 42.1% (whole sample: long 51.7%).

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| top decile long: gross signed | 288 | +14.7 | +33.2 | 226.4 | 58% | +1.10 | +0.82 | +0.77 |
| top decile long: net | 288 | -105.3 | -86.8 | 226.4 | 29% | -7.89 | -5.90 | -5.56 |
| top decile short: gross signed | 288 | -28.1 | -41.9 | 200.2 | 41% | -2.38 | -1.85 | -1.79 |
| top decile short: net | 288 | -178.1 | -191.9 | 200.2 | 16% | -15.09 | -11.75 | -11.35 |

## 6. EXPLORATORY — delay buckets and thesis subset (delay > 0.3)

### bucket A  (rows 115,069)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC — signed | 166 | -0.0041 | -0.0052 | 0.0694 | 46% | -0.75 | -0.67 | -0.67 |
| composite IC — raw | 166 | -0.0017 | -0.0004 | 0.0874 | 50% | -0.25 | -0.19 | -0.19 |
| volume IC — signed | 166 | +0.0045 | +0.0050 | 0.0653 | 52% | +0.88 | +0.79 | +0.78 |
| volume IC — raw | 166 | +0.0018 | +0.0055 | 0.0785 | 52% | +0.29 | +0.21 | +0.20 |
| return_mag IC — signed | 166 | -0.0145 | -0.0109 | 0.0959 | 43% | -1.95 | -2.04 | -2.15 |
| return_mag IC — raw | 166 | -0.0047 | -0.0015 | 0.0823 | 49% | -0.74 | -0.68 | -0.69 |
| sector_rs IC — signed | 166 | +0.0005 | +0.0027 | 0.0582 | 52% | +0.12 | +0.11 | +0.11 |
| sector_rs IC — raw | 166 | -0.0031 | -0.0011 | 0.0591 | 49% | -0.68 | -0.52 | -0.51 |
| delay IC — signed | 166 | +0.0075 | +0.0127 | 0.0701 | 57% | +1.38 | +1.43 | +1.56 |
| delay IC — raw | 166 | -0.0037 | +0.0020 | 0.1123 | 51% | -0.43 | -0.30 | -0.30 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite D10 − D1 signed | 166 | +17.5 | +13.2 | 170.5 | 53% | +1.32 | +1.28 | +1.23 |
| composite D10 − rest signed | 166 | +23.9 | +30.1 | 144.3 | 61% | +2.13 | +2.10 | +2.08 |
| top-decile gross signed | 166 | +19.3 | +27.5 | 152.0 | 61% | +1.63 | +1.57 | +1.54 |
| top-decile NET | 166 | -112.8 | -106.5 | 152.2 | 19% | -9.55 | -9.15 | -8.97 |

### bucket B  (rows 76,354)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC — signed | 166 | -0.0038 | -0.0040 | 0.0714 | 47% | -0.69 | -0.68 | -0.72 |
| composite IC — raw | 166 | -0.0057 | -0.0028 | 0.0801 | 49% | -0.92 | -0.67 | -0.64 |
| volume IC — signed | 166 | +0.0033 | +0.0065 | 0.0746 | 52% | +0.57 | +0.58 | +0.59 |
| volume IC — raw | 166 | -0.0092 | -0.0046 | 0.0880 | 46% | -1.35 | -0.92 | -0.87 |
| return_mag IC — signed | 166 | -0.0124 | -0.0147 | 0.0872 | 42% | -1.83 | -1.91 | -2.04 |
| return_mag IC — raw | 166 | -0.0006 | +0.0036 | 0.0686 | 51% | -0.10 | -0.10 | -0.10 |
| sector_rs IC — signed | 166 | +0.0049 | +0.0055 | 0.0639 | 51% | +0.98 | +0.98 | +1.06 |
| sector_rs IC — raw | 166 | +0.0002 | +0.0016 | 0.0646 | 51% | +0.03 | +0.02 | +0.02 |
| delay IC — signed | 166 | +0.0005 | +0.0014 | 0.0502 | 51% | +0.13 | +0.13 | +0.13 |
| delay IC — raw | 166 | +0.0018 | +0.0016 | 0.0572 | 51% | +0.40 | +0.28 | +0.28 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite D10 − D1 signed | 166 | +7.8 | +11.0 | 179.5 | 53% | +0.56 | +0.50 | +0.49 |
| composite D10 − rest signed | 166 | +16.6 | +12.2 | 154.6 | 54% | +1.38 | +1.27 | +1.28 |
| top-decile gross signed | 166 | +11.6 | +8.0 | 161.4 | 52% | +0.93 | +0.85 | +0.84 |
| top-decile NET | 166 | -119.9 | -123.1 | 162.7 | 18% | -9.50 | -8.70 | -8.66 |

### bucket C  (rows 184,941)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC — signed | 166 | -0.0046 | -0.0074 | 0.0684 | 48% | -0.87 | -0.77 | -0.74 |
| composite IC — raw | 166 | -0.0016 | +0.0021 | 0.0739 | 52% | -0.28 | -0.22 | -0.21 |
| volume IC — signed | 166 | -0.0011 | +0.0027 | 0.0667 | 52% | -0.21 | -0.19 | -0.19 |
| volume IC — raw | 166 | -0.0004 | +0.0079 | 0.0713 | 58% | -0.08 | -0.05 | -0.05 |
| return_mag IC — signed | 166 | -0.0081 | -0.0074 | 0.0922 | 48% | -1.14 | -1.21 | -1.23 |
| return_mag IC — raw | 166 | -0.0024 | -0.0052 | 0.0727 | 46% | -0.43 | -0.46 | -0.47 |
| sector_rs IC — signed | 166 | +0.0049 | +0.0091 | 0.0520 | 57% | +1.22 | +1.06 | +1.07 |
| sector_rs IC — raw | 166 | -0.0028 | +0.0010 | 0.0493 | 51% | -0.74 | -0.55 | -0.53 |
| delay IC — signed | 166 | -0.0026 | -0.0043 | 0.0398 | 48% | -0.83 | -0.81 | -0.81 |
| delay IC — raw | 166 | -0.0053 | -0.0048 | 0.0404 | 46% | -1.68 | -1.14 | -1.09 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite D10 − D1 signed | 166 | +4.9 | +11.0 | 115.5 | 53% | +0.55 | +0.50 | +0.47 |
| composite D10 − rest signed | 166 | +2.1 | +12.1 | 85.6 | 55% | +0.31 | +0.27 | +0.25 |
| top-decile gross signed | 166 | -3.0 | +1.9 | 112.6 | 51% | -0.35 | -0.34 | -0.32 |
| top-decile NET | 166 | -135.3 | -131.0 | 114.0 | 9% | -15.29 | -14.84 | -14.00 |

### bucket D  (rows 178,191)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC — signed | 166 | -0.0053 | -0.0035 | 0.0878 | 49% | -0.78 | -0.84 | -0.87 |
| composite IC — raw | 166 | -0.0070 | -0.0067 | 0.0872 | 47% | -1.03 | -0.85 | -0.81 |
| volume IC — signed | 166 | -0.0022 | +0.0078 | 0.0677 | 55% | -0.41 | -0.40 | -0.42 |
| volume IC — raw | 166 | -0.0036 | +0.0036 | 0.0743 | 52% | -0.63 | -0.45 | -0.42 |
| return_mag IC — signed | 166 | -0.0080 | -0.0150 | 0.1283 | 45% | -0.81 | -0.98 | -1.00 |
| return_mag IC — raw | 166 | -0.0066 | -0.0008 | 0.1076 | 49% | -0.79 | -0.75 | -0.76 |
| sector_rs IC — signed | 166 | +0.0053 | +0.0021 | 0.0536 | 54% | +1.27 | +1.12 | +1.12 |
| sector_rs IC — raw | 166 | -0.0040 | -0.0035 | 0.0453 | 47% | -1.13 | -0.93 | -0.91 |
| delay IC — signed | 166 | -0.0061 | -0.0103 | 0.1136 | 47% | -0.69 | -0.68 | -0.66 |
| delay IC — raw | 166 | -0.0181 | -0.0321 | 0.1158 | 42% | -2.02 | -1.38 | -1.35 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite D10 − D1 signed | 166 | -8.5 | -4.1 | 103.8 | 48% | -1.05 | -1.02 | -1.04 |
| composite D10 − rest signed | 166 | -4.4 | -2.8 | 73.3 | 46% | -0.77 | -0.75 | -0.76 |
| top-decile gross signed | 166 | -7.9 | -1.8 | 128.5 | 49% | -0.79 | -0.81 | -0.83 |
| top-decile NET | 166 | -140.3 | -132.8 | 130.4 | 11% | -13.86 | -14.27 | -14.60 |

### bucket E  (rows 397,447)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC — signed | 288 | -0.0040 | -0.0038 | 0.1612 | 49% | -0.42 | -0.41 | -0.39 |
| composite IC — raw | 288 | -0.0096 | -0.0136 | 0.1389 | 45% | -1.18 | -0.89 | -0.86 |
| volume IC — signed | 288 | -0.0043 | -0.0065 | 0.1516 | 47% | -0.48 | -0.44 | -0.42 |
| volume IC — raw | 288 | -0.0046 | -0.0072 | 0.1386 | 47% | -0.57 | -0.41 | -0.39 |
| return_mag IC — signed | 288 | -0.0031 | +0.0030 | 0.1791 | 51% | -0.29 | -0.30 | -0.29 |
| return_mag IC — raw | 288 | -0.0094 | -0.0177 | 0.1312 | 47% | -1.21 | -1.08 | -1.07 |
| sector_rs IC — signed | 288 | +0.0067 | +0.0010 | 0.1055 | 51% | +1.08 | +1.05 | +1.06 |
| sector_rs IC — raw | 288 | +0.0024 | +0.0032 | 0.1121 | 53% | +0.37 | +0.29 | +0.30 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite D10 − D1 signed | 122 | -13.1 | -1.2 | 111.9 | 49% | -1.29 | -1.41 | -1.49 |
| composite D10 − rest signed | 122 | -8.8 | +1.3 | 84.5 | 52% | -1.15 | -1.10 | -1.14 |
| top-decile gross signed | 122 | -13.5 | +5.6 | 188.7 | 52% | -0.79 | -0.97 | -0.98 |
| top-decile NET | 122 | -146.9 | -128.0 | 189.0 | 16% | -8.58 | -10.33 | -10.40 |

### thesis (delay > 0.3)  (rows 191,423)

| series | days | mean | median | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite IC — signed | 166 | -0.0032 | +0.0002 | 0.0595 | 51% | -0.70 | -0.65 | -0.66 |
| composite IC — raw | 166 | -0.0070 | +0.0002 | 0.0812 | 51% | -1.10 | -0.79 | -0.77 |
| volume IC — signed | 166 | +0.0043 | +0.0039 | 0.0594 | 54% | +0.93 | +0.90 | +0.90 |
| volume IC — raw | 166 | -0.0033 | +0.0005 | 0.0752 | 50% | -0.56 | -0.38 | -0.37 |
| return_mag IC — signed | 166 | -0.0138 | -0.0095 | 0.0836 | 45% | -2.13 | -2.28 | -2.48 |
| return_mag IC — raw | 166 | -0.0029 | +0.0003 | 0.0675 | 50% | -0.55 | -0.53 | -0.54 |
| sector_rs IC — signed | 166 | +0.0029 | -0.0007 | 0.0507 | 50% | +0.75 | +0.68 | +0.72 |
| sector_rs IC — raw | 166 | -0.0010 | -0.0009 | 0.0532 | 50% | -0.25 | -0.18 | -0.17 |
| delay IC — signed | 166 | +0.0062 | +0.0046 | 0.0623 | 53% | +1.28 | +1.34 | +1.39 |
| delay IC — raw | 166 | -0.0189 | -0.0142 | 0.1156 | 46% | -2.10 | -1.46 | -1.44 |

| series | days | mean (bps) | median (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|---|
| composite D10 − D1 signed | 166 | +15.8 | +19.6 | 151.1 | 55% | +1.34 | +1.20 | +1.16 |
| composite D10 − rest signed | 166 | +21.8 | +23.6 | 131.3 | 59% | +2.14 | +1.94 | +1.90 |
| top-decile gross signed | 166 | +17.0 | +22.6 | 138.8 | 58% | +1.58 | +1.47 | +1.44 |
| top-decile NET | 166 | -114.8 | -105.6 | 139.2 | 17% | -10.62 | -9.87 | -9.70 |

Secondary cells with a signed IC at NW3 t ≤ −2.0 (exploratory):
- bucket A, return_mag signed IC: mean -0.0145, NW3 t -2.04
- thesis (delay > 0.3), return_mag signed IC: mean -0.0138, NW3 t -2.28
Whole-sample signed ICs at NW3 t ≤ −2.0: none.

## 7. EXPLORATORY — between-day / within-day decomposition of the pooled signed IC

| score | pooled signed IC | between-day part | within-day part | pooled IC, sfwd demeaned by day | mean per-day signed IC |
|---|---|---|---|---|---|
| composite | -0.0052 | -0.0017 | -0.0035 | -0.0023 | -0.0041 |
| volume | -0.0037 | -0.0010 | -0.0028 | -0.0014 | -0.0031 |
| return_mag | -0.0075 | -0.0036 | -0.0039 | -0.0032 | -0.0051 |
| sector_rs | +0.0019 | +0.0012 | +0.0007 | +0.0006 | +0.0008 |
| delay | -0.0007 | -0.0006 | -0.0001 | -0.0013 | -0.0014 |

Control, composite vs raw fwd on the same rows: pooled +0.0109 = between +0.0168 + within -0.0059.

## 8. Matched vs unmatched rows (descriptive, no returns)

| group | rows | composite mean | median | p10 / p90 | % in within-day top decile | % delay non-NULL | delay mean | delay median |
|---|---|---|---|---|---|---|---|---|
| used in primary sample | 952,002 | 0.4987 | 0.4943 | 0.294 / 0.712 | 10.1% | 58.3% | 0.286 | 0.183 |
| matched, DLYRETX = 0 (dropped) | 12,502 | 0.3428 | 0.3401 | 0.198 / 0.489 | 0.2% | 62.9% | 0.446 | 0.351 |
| no PERMNO | 30,984 | 0.4947 | 0.4917 | 0.296 / 0.700 | 10.1% | 59.8% | 0.334 | 0.223 |
| identity check failed | 1,540 | 0.5291 | 0.5323 | 0.314 / 0.741 | 15.5% | 54.0% | 0.370 | 0.297 |
| PERMNO but no CRSP row | 1,145 | 0.5122 | 0.5042 | 0.312 / 0.726 | 12.8% | 48.5% | 0.351 | 0.225 |
| excluded period 2025-12-29 → 2026-04-15 | 291,699 | 0.4972 | 0.4961 | 0.300 / 0.696 | 10.0% | 97.8% | 0.274 | 0.172 |

Top-decile membership uses deciles formed on all CSV rows of each day. A share of 10% means no over/under-representation.

_Runtime 20s._
