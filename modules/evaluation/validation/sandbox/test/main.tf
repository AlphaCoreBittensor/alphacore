terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "google" {
  project = "alphacore-478113"
  region  = "us-east1"
}


resource "google_compute_instance" "main_0" {
  name = "vm-0579b710"
  zone = "us-central1-a"
  machine_type = "e2-small"
  metadata_startup_script = <<-EOT
    #!/bin/bash
    printf '596ea7-0579b7' > /var/tmp/acore-token
  EOT

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network = "default"
  }
}
