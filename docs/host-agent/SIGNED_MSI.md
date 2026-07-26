# STOIC — Signed MSI Installer Pipeline (WiX + Authenticode)

Completes the end-to-end artifact chain:

```
CI Build → Signed EX5 (Ed25519 manifest) → Signed MSI (Authenticode)
        → Signed Manifest → SHA-256 verify → Host Agent validation → Deploy
```

## Prerequisite (one-time purchase — cannot be automated)
Buy a **Windows code-signing certificate**:
- Standard OV cert (~$200–400/yr: Sectigo, DigiCert, SSL.com) — SmartScreen
  reputation builds over time, or
- **EV cert / Azure Trusted Signing** (~$10/mo, recommended) — instant
  SmartScreen reputation, cloud-held key (no USB token in CI).

Store credentials as GitHub secrets: `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `TRUSTED_SIGNING_ACCOUNT`,
`TRUSTED_SIGNING_PROFILE` (Trusted Signing) — or `SIGN_CERT_PFX_B64` +
`SIGN_CERT_PASSWORD` (classic PFX; less safe, avoid if possible).

## 1. WiX v4 project (`installer/stoic-agent.wxs`)
```xml
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="STOIC Host Agent" Manufacturer="STOIC"
           Version="2.1.0" UpgradeCode="PUT-A-FIXED-GUID-HERE">
    <MajorUpgrade DowngradeErrorMessage="Newer version already installed." />
    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLDIR" Name="Stoic">
        <Component Id="AgentScript">
          <File Source="docs/host-agent/stoic-host-agent.ps1" />
        </Component>
        <Component Id="AgentService">
          <!-- register + start the service on install -->
          <ServiceInstall Id="Svc" Name="StoicHostAgent" DisplayName="STOIC Host Agent"
                          Start="auto" Type="ownProcess" ErrorControl="normal"
                          Arguments="-ExecutionPolicy Bypass -NoProfile -File &quot;[INSTALLDIR]stoic-host-agent.ps1&quot; -Run" />
          <ServiceControl Id="SvcCtl" Name="StoicHostAgent" Start="install" Stop="both" Remove="uninstall" Wait="yes" />
          <File Source="installer/service-shim.exe" KeyPath="yes" />
        </Component>
      </Directory>
    </StandardDirectory>
    <Feature Id="Main"><ComponentRef Id="AgentScript" /><ComponentRef Id="AgentService" /></Feature>
  </Package>
</Wix>
```

## 2. GitHub Actions job (append to `.github/workflows/release.yml`)
```yaml
  msi-build-sign:
    runs-on: windows-latest
    needs: [backend-unit, ea-structural-check]
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803  # v6
      - name: Build MSI (WiX v4)
        run: |
          dotnet tool install --global wix
          wix build installer/stoic-agent.wxs -o dist/stoic-agent.msi
      - name: Sign MSI (Azure Trusted Signing)
        uses: azure/trusted-signing-action@v0.5.1
        with:
          azure-tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          azure-client-id: ${{ secrets.AZURE_CLIENT_ID }}
          azure-client-secret: ${{ secrets.AZURE_CLIENT_SECRET }}
          endpoint: https://eus.codesigning.azure.net/
          trusted-signing-account-name: ${{ secrets.TRUSTED_SIGNING_ACCOUNT }}
          certificate-profile-name: ${{ secrets.TRUSTED_SIGNING_PROFILE }}
          files-folder: dist
          files-folder-filter: msi
          file-digest: SHA256
          timestamp-rfc3161: http://timestamp.acs.microsoft.com
      - name: Verify signature + emit SHA-256
        run: |
          signtool verify /pa /v dist/stoic-agent.msi
          $h = (Get-FileHash dist/stoic-agent.msi -Algorithm SHA256).Hash.ToLower()
          echo "MSI_SHA256=$h" >> $env:GITHUB_ENV
          echo $h > dist/stoic-agent.msi.sha256
      - uses: actions/upload-artifact@v4
        with: { name: stoic-agent-msi, path: dist/ }
```

## 3. Manifest inclusion (backend)
After CI produces the MSI, register it as a content-addressed artifact so it
appears in the Ed25519-signed manifest with `type: "msi"`:
- copy `stoic-agent.msi` into the backend artifact store (same flow the EX5
  uses in `vps_pathb.build_artifact_manifest`), keyed by its SHA-256;
- agents/downloaders then fetch `/api/artifacts/{sha256}` — never a mutable
  URL — and verify BOTH the Authenticode signature (OS-level) and the
  manifest SHA-256 (app-level) before executing.

## 4. Verification chain on the VPS
1. Windows verifies the Authenticode signature at install (SmartScreen).
2. The Host Agent verifies the Ed25519 manifest with its PINNED public key.
3. Every artifact download is SHA-256-checked before swap; failed swaps
   roll back automatically (`.bak` restore — implemented in agent v2.1).
