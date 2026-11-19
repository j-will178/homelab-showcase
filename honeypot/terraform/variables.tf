variable "region" {
  description = "DigitalOcean region for both droplets (and the Packer image)."
  type        = string
  default     = "nyc3"
}

variable "home_cidrs" {
  description = "Where the lab connects from: the only sources allowed to the bastion's SSH and the image build droplet. Passed at apply time (looked up by the rebuild script), never committed."
  type        = list(string)
  validation {
    condition     = length(var.home_cidrs) > 0 && alltrue([for c in var.home_cidrs : can(cidrnetmask(c)) && !contains(["0.0.0.0/0"], c)])
    error_message = "home_cidrs must be one or more IPv4 CIDRs, and not 0.0.0.0/0."
  }
}

variable "exposure" {
  description = <<-EOT
    Who can reach Cowrie on 22/23. "lab-only": only home_cidrs, for testing the whole pipeline
    before real attackers arrive. "public": the internet. Going live (or back) is a one-line PR
    changing this default.
  EOT
  type        = string
  default     = "lab-only"
  validation {
    condition     = contains(["lab-only", "public"], var.exposure)
    error_message = "exposure must be \"lab-only\" or \"public\"."
  }
}

variable "honeypot_image" {
  description = "Name of the Packer snapshot to run, e.g. hp-honeypot-20261118-0930 (Packer's manifest.json)."
  type        = string
}

variable "bastion_image" {
  type    = string
  default = "debian-13-x64"
}

variable "bastion_size" {
  type    = string
  default = "s-1vcpu-512mb-10gb"
}

variable "honeypot_size" {
  type    = string
  default = "s-1vcpu-1gb"
}

variable "bastion_vpc_range" {
  type    = string
  default = "172.30.10.0/24"
}

variable "honeypot_vpc_range" {
  type    = string
  default = "172.30.20.0/24"
}

variable "lab_ssh_public_key" {
  description = "cloud-ops's key: 'jump' on the bastion (forward to :47022 only) and 'collector' on the honeypot (log reader only)."
  type        = string
}

variable "admin_ssh_public_key" {
  description = "Your own key: 'hpadmin' (sudo) on both droplets, through the bastion."
  type        = string
}

# Host keys are generated in the lab and injected, so the lab knows each droplet's identity in
# advance (pinned in known_hosts), including every weekly replacement of the honeypot.
variable "bastion_host_key" {
  description = "Bastion SSH host key: { private = <OpenSSH ed25519 private key>, public = <ssh-ed25519 ...> }."
  type        = object({ private = string, public = string })
  sensitive   = true
}

variable "honeypot_host_key" {
  description = "Honeypot SSH host key, new for each rebuild: { private = ..., public = ... }."
  type        = object({ private = string, public = string })
  sensitive   = true
}
