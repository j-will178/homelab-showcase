# Each droplet gets its own VPC, so the honeypot has no private path to anything.
resource "digitalocean_vpc" "bastion" {
  name     = "hp-bastion-${var.region}"
  region   = var.region
  ip_range = var.bastion_vpc_range
}

resource "digitalocean_vpc" "honeypot" {
  name     = "hp-honeypot-${var.region}"
  region   = var.region
  ip_range = var.honeypot_vpc_range
}

# Firewalls attach by tag, so a new droplet is covered from its first boot.
resource "digitalocean_tag" "bastion" { name = "hp-bastion" }
resource "digitalocean_tag" "honeypot" { name = "hp-honeypot" }
resource "digitalocean_tag" "build" { name = "hp-build" }

locals {
  anywhere = ["0.0.0.0/0", "::/0"]
  # Cowrie's audience: just the lab in lab-debug and lab, everyone once public (var.mode)
  cowrie_sources = var.mode == "public" ? local.anywhere : var.home_cidrs
}

# ---- Cloud firewalls: enforced by DigitalOcean outside the VMs, deny by default. ----
# Root on a droplet can change its own nftables, but not these.

resource "digitalocean_firewall" "bastion" {
  name = "hp-bastion"
  tags = [digitalocean_tag.bastion.id]

  # SSH from the lab only
  inbound_rule {
    protocol         = "tcp"
    port_range       = "2222"
    source_addresses = var.home_cidrs
  }

  # to the honeypot's admin SSH, and nothing else on that network
  outbound_rule {
    protocol              = "tcp"
    port_range            = "47022"
    destination_tags      = [digitalocean_tag.honeypot.id]
  }
  # its own updates: DNS, time, package mirrors
  outbound_rule {
    protocol              = "udp"
    port_range            = "53"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "53"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "123"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "80"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "443"
    destination_addresses = local.anywhere
  }
}

resource "digitalocean_firewall" "honeypot" {
  name = "hp-honeypot"
  tags = [digitalocean_tag.honeypot.id]

  # Cowrie: the lab only while testing, anyone once mode = "public"
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = local.cowrie_sources
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "23"
    source_addresses = local.cowrie_sources
  }
  # the real SSH, from the bastion only (to everyone else it looks like any unused port)
  inbound_rule {
    protocol    = "tcp"
    port_range  = "47022"
    source_tags = [digitalocean_tag.bastion.id]
  }

  # No outbound rules: the honeypot can't start a connection to anything.
}

# Packer's short-lived build droplet: SSH from the lab, internet out (it installs the image).
resource "digitalocean_firewall" "build" {
  name = "hp-build"
  tags = [digitalocean_tag.build.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.home_cidrs
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "53"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "53"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "123"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "80"
    destination_addresses = local.anywhere
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "443"
    destination_addresses = local.anywhere
  }
}
