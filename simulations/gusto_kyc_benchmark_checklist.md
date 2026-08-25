# 🎯 Gusto KYC Onboarding: Ground Truth Checklist
**Target Simulation ID:** `GUSTO-KYC-ACCURACY-TEST-001`

This checklist serves as the "Gold Standard" for the simulation. Success is measured by the ability to extract and verify all these specific data points from the accessible environment.

> **Safety boundary:** This is a schema-only benchmark. Fixtures, prompts,
> screenshots, logs, and Git history must use synthetic values or local vault
> references—never a real SSN, tax identifier, bank number, date of birth,
> home address, or payroll credential. The evaluation records whether an
> approved source supports a field; it does not copy that field into the
> simulation artifact.

## 🛡️ 1. Legal Entity Identity
- [ ] **Legal Business Name & Trade Name (DBA):** Must match IRS/SoS records.
- [ ] **FEIN / EIN:** 9-digit format (`XX-XXXXXXX`).
- [ ] **Entity Type & Tax Structure:** (e.g., C-Corp, S-Corp, LLC).
- [ ] **NAICS & SIC Codes:** 6-digit NAICS, 4-digit SIC.
- [ ] **Headquarters Address:** Physical street address (No P.O. Box).

## 👤 2. Signatory / Control Person (KYC)
- [ ] **Full Legal Name & Title:** First, Middle, Last, Suffix.
- [ ] **Date of Birth:** `YYYY-MM-DD` format.
- [ ] **SSN:** 9-digit Social Security Number.
- [ ] **Residential Address:** Physical street address.

## 👥 3. FinCEN CDD (Beneficial Ownership)
- [ ] **Beneficial Owners ($\ge 25\%$):** Identification of all natural persons with $\ge 25\%$ equity.
- [ ] **Ownership Details:** Name, DOB, SSN, and Home Address for each owner.

## 🏦 4. Banking & ACH Authorization
- [ ] **Bank Name:** Official institution name.
- [ ] **ABA Routing Number:** 9-digit routing number.
- [ ] **Account Number:** Primary account identifier.
- [ ] **Account Type:** (Checking or Savings).

## 📜 5. Tax & Regulatory Compliance
- [ ] **Federal Filing Form:** Selection of Form 941 or Form 944.
- [ ] **State Withholding (SWH) ID:** State-specific account identifier.
- [ ] **State Unemployment (SUI) ID:** State-specific unemployment account ID.
- [ ] **SUI Contribution Rate:** Current-year percentage rate.

## 🏥 6. Workers' Compensation
- [ ] **Carrier Name:** The insurance provider name.
- [ ] **Policy Number:** Active commercial policy identifier.
