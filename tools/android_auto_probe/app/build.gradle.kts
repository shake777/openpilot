plugins {
  id("com.android.application")
  id("org.jetbrains.kotlin.android")
}

android {
  namespace = "com.carrot.aaprobe"
  compileSdk = 34

  defaultConfig {
    applicationId = "com.carrot.aaprobe"
    minSdk = 26
    targetSdk = 34
    versionCode = 3
    versionName = "0.3"
  }

  buildTypes {
    release {
      isMinifyEnabled = false
      // Test tool only: sign release with the debug key so it installs without extra setup.
      signingConfig = signingConfigs.getByName("debug")
    }
  }

  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  kotlinOptions {
    jvmTarget = "17"
  }
}

dependencies {
  implementation("androidx.car.app:app:1.4.0")
  // Android Auto (projected) implementation of CarHardwareManager; without it sensors are unavailable.
  implementation("androidx.car.app:app-projected:1.4.0")
  implementation("androidx.core:core-ktx:1.13.1")
}
