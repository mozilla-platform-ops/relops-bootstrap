variable "project_id" {
  description = "GCP project ID for the relops-bootstrap infra"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run, Artifact Registry, step-ca VM"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for the step-ca VM"
  type        = string
  default     = "us-central1-a"
}

# Puppet roles that need a vault secret. One Secret Manager container per entry.
# Mirrors data/roles/*.yaml in ronin_puppet. Keep in sync when roles change.
variable "puppet_roles" {
  description = "Puppet roles for which we provision a vault.yaml Secret Manager container"
  type        = list(string)
  default = [
    # macOS hardware
    "gecko_t_osx_1500_m4",
    "gecko_t_osx_1500_m4_no_sip",
    "gecko_t_osx_1500_m4_staging",
    "gecko_t_osx_1400_r8",
    "gecko_t_osx_1015",

    # Linux hardware (per modules/roles_profiles/manifests/roles/gecko_t_linux_*.pp in ronin_puppet)
    "gecko_t_linux_talos",
    "gecko_t_linux_2204_talos",
    "gecko_t_linux_2404_talos",
    "gecko_t_linux_2404_talos_wayland",
    "gecko_t_linux_netperf",
    "gecko_t_linux_2404_netperf",

    # add other roles here as the migration progresses
  ]
}

variable "vault_broker_image" {
  description = "Container image for the vault-broker Cloud Run service. Defaults to GCP's hello image until the real broker is built."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "vault_broker_min_instances" {
  description = "Cloud Run min instances. Start at 0 to save cost during development."
  type        = number
  default     = 0
}

variable "vault_broker_max_instances" {
  description = "Cloud Run max instances. EACS is low-frequency; broker doesn't need to scale."
  type        = number
  default     = 5
}

variable "step_ca_machine_type" {
  description = "Machine type for the step-ca GCE VM. e2-small is fine for CA workload."
  type        = string
  default     = "e2-small"
}

variable "trusted_source_cidrs" {
  description = "Source CIDRs allowed to call the broker (MDC1 worker network). Hosts outside these ranges are rejected at the LB layer."
  type        = list(string)
  default     = []
  # populate via terraform.tfvars, e.g. ["10.49.0.0/16"]
}

variable "broker_hostname" {
  description = "FQDN for the vault-broker HTTPS endpoint. Empty disables the LB cert + forwarding rule (the rest of the LB is still built so we can attach the cert when ready)."
  type        = string
  default     = ""
}
