terraform {
  # Runs on optiplex-management. Also works with OpenTofu.
  required_version = ">= 1.16.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.95"
    }
  }
}

# Credentials: DIGITALOCEAN_TOKEN in the environment (never in git or in tfvars).
provider "digitalocean" {}
