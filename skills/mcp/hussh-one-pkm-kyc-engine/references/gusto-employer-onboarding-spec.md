# Gusto Employer Onboarding Specification & Document Checklist

## Overview
This specification details all compliance, tax, banking, and identity requirements for onboarding an employer onto Gusto (direct web portal or Gusto Embedded Payroll API).

---

## 1. Compliance & Underwriting Regimes
1. **IRS Federal Tax Mandates:** Form 8655 reporting agent authorization, 941/944 employer quarterly/annual tax returns.
2. **FinCEN CDD (Customer Due Diligence) Rule:** Verification of all natural persons owning $\ge 25\%$ equity, plus identification of at least one designated Control Person.
3. **Nacha ACH Debit Origination Rules:** Direct bank account verification (Plaid tokenized or micro-deposits) with signed debit authorization.
4. **State Unemployment & Withholding Departments:** Registration IDs and current year contribution tax rates for every state where employees reside.

---

## 2. Employer Onboarding Checklist & Document Matrix

| Category | Required Field / Data Item | Format / Rules | Required Source Document |
| :--- | :--- | :--- | :--- |
| **Legal Entity** | Legal Business Name | Exactly as filed with IRS/SoS | IRS CP 575 or Form 147C |
| **Legal Entity** | FEIN / EIN | 9 digits (XX-XXXXXXX) | IRS CP 575 Notice |
| **Legal Entity** | Entity Type & Tax Structure | C-Corp, S-Corp, LLC, Partnership | Articles of Organization / Formation |
| **Legal Entity** | NAICS & SIC Code | 6-digit NAICS, 4-digit SIC | State business license / tax filing |
| **Legal Entity** | Company Headquarters Address | Physical street address (No P.O. Box) | Lease, utility bill, or tax notice |
| **Signatory (KYC)** | Full Legal Name | First, Middle, Last, Suffix | Passport or State Driver's License |
| **Signatory (KYC)** | Date of Birth | `YYYY-MM-DD` | Passport or Driver's License |
| **Signatory (KYC)** | Social Security Number | 9 digits (XXX-XX-XXXX) | Social Security Card |
| **Signatory (KYC)** | Residential Address | Physical street address | Utility bill / bank statement |
| **Signatory (KYC)** | Title / Executive Role | CEO, President, Managing Member | Operating Agreement / Cap Table |
| **FinCEN CDD** | Beneficial Owners ($\ge 25\%$) | Name, DOB, SSN, Residential Address | Cap Table / Government ID scans |
| **Banking (ACH)** | Bank Name & Routing Number | 9-digit ABA routing number | Voided Check or Bank Letter |
| **Banking (ACH)** | Account Number & Type | Commercial Checking or Savings | Voided Check or Bank Statement |
| **Federal Tax** | Filing Form Selection | Form 941 or Form 944 | Prior year tax return |
| **State Tax (SWH)** | State Withholding Account ID | Format varies by state | State Dept of Revenue Notice |
| **State Tax (SUI)** | State Unemployment Account ID | Format varies by state | State Dept of Labor Notice |
| **State Tax (SUI)** | SUI Tax Contribution Rate | E.g., 2.70%, 3.40% | Annual State SUI Rate Notice |
| **Workers' Comp** | Carrier Name & Policy Number | Active commercial policy # | Certificate of Insurance (COI) |
| **Pay Schedule** | Frequency & Anchor Dates | Weekly, Bi-weekly, Semi-monthly | Corporate payroll policy |

---

## 3. Gusto Embedded Payroll API Payload Mapping

### Company Creation
`POST /v1/companies`
```json
{
  "name": "Hushh Technologies LLC",
  "trade_name": "Hussh"
}
```

### Federal Tax Details
`PUT /v1/companies/{company_uuid}/federal_tax_details`
```json
{
  "legal_name": "Hushh Technologies LLC",
  "ein": "123456789",
  "tax_payer_type": "LLC",
  "taxable_as_scorp": false,
  "filing_form": "941"
}
```

### Industry Selection
`PUT /v1/companies/{company_uuid}/industry_selection`
```json
{
  "naics_code": "541511",
  "sic_codes": ["7371"]
}
```

### Signatory Creation
`POST /v1/companies/{company_uuid}/signatories`
```json
{
  "first_name": "Kushal",
  "last_name": "Trivedi",
  "title": "CEO",
  "email": "kushaltrivedi1711@gmail.com",
  "phone": "2012419368",
  "birthday": "1990-01-01",
  "ssn": "123456789",
  "home_address": {
    "street_1": "123 Main St",
    "city": "Jersey City",
    "state": "NJ",
    "zip": "07302"
  }
}
```

### Bank Account Verification
`POST /v1/companies/{company_uuid}/bank_accounts`
```json
{
  "routing_number": "123456789",
  "account_number": "9876543210",
  "account_type": "Checking"
}
```

### State Tax Requirements
`PUT /v1/companies/{company_uuid}/tax_requirements/{state}`
```json
{
  "requirement_id": "state_withholding_id",
  "value": "123456"
}
```
