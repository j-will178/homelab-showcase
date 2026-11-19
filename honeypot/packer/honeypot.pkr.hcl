# Builds the honeypot image: patched Debian 13 + Cowrie (pinned, hash-checked) + hardening.
# Built on a short-lived droplet that DOES have internet access; the honeypot itself never does,
# so everything it needs is baked in here. Run by cloud-ops weekly (see README).
#
#   export DIGITALOCEAN_TOKEN=...      # never in git
#   packer init . && packer build -var region=nyc3 .

packer {
  required_version = ">= 1.15.0"
  required_plugins {
    digitalocean = {
      source  = "github.com/digitalocean/digitalocean"
      version = ">= 1.3.1"
    }
  }
}

variable "region" {
  type    = string
  default = "nyc3"
}

variable "base_image" {
  type    = string
  default = "debian-13-x64"
}

variable "build_size" {
  type    = string
  default = "s-1vcpu-1gb"
}

locals {
  stamp = formatdate("YYYYMMDD-hhmm", timestamp())
}

source "digitalocean" "honeypot" {
  # api_token comes from DIGITALOCEAN_TOKEN
  image         = var.base_image
  region        = var.region
  size          = var.build_size
  ssh_username  = "root"
  droplet_name  = "hp-build-${local.stamp}"
  snapshot_name = "hp-honeypot-${local.stamp}"
  snapshot_regions = [var.region]
  # The "hp-build" tag gets a cloud firewall from Terraform: SSH in from home only, internet out.
  tags          = ["hp-build"]
  monitoring    = false
}

build {
  sources = ["source.digitalocean.honeypot"]

  provisioner "file" {
    source      = "files"
    destination = "/tmp"
  }

  provisioner "shell" {
    script = "scripts/build.sh"
  }

  # The image name, for Terraform: terraform apply -var honeypot_image=<name>
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
