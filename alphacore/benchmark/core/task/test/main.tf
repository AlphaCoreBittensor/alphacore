terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = "alphacore-478113"
  region  = "us-east1"
  zone    = "us-east1-c"
}

resource "google_compute_instance" "vm" {
  name         = "vm-24e65ab6f232ce80"
  machine_type = "e2-small"

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }
}
