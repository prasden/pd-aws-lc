// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0 OR ISC

#include <gtest/gtest.h>
#include <openssl/ssl.h>
#include "internal.h"

// These tests connect to a live, remote host, so they are built into the
// integration_test executable rather than tool_openssl_test. See
// ssl/CMakeLists.txt.

// Test -connect
TEST(SClientIntegrationTest, Connect) {
  args_list_t args = {"-connect", "amazon.com:443"};
  int result = SClientTool(args);
  ASSERT_EQ(kToolExitSuccess, result);
}

// Test -connect, -verify, -showcerts
TEST(SClientIntegrationTest, ConnectVerifyShowcerts) {
  args_list_t args = {"-connect", "amazon.com:443", "-verify", "99", "-showcerts"};
  int result = SClientTool(args);
  ASSERT_EQ(kToolExitSuccess, result);
}

// Test -cipher
TEST(SClientIntegrationTest, Cipher) {
  // Pin to TLS 1.2 so the -cipher list is actually enforced. Without a version
  // pin the handshake can negotiate TLS 1.3, whose cipher suites are configured
  // separately, leaving -cipher effectively ignored.
  args_list_t args = {"-connect", "amazon.com:443", "-cipher",
                      "ECDHE-RSA-AES128-GCM-SHA256", "-tls1_2"};
  int result = SClientTool(args);
  ASSERT_EQ(kToolExitSuccess, result);
}

// Test -tls1_3
TEST(SClientIntegrationTest, Tls1_3) {
  args_list_t args = {"-connect", "amazon.com:443", "-tls1_3"};
  int result = SClientTool(args);
  ASSERT_EQ(kToolExitSuccess, result);
}
