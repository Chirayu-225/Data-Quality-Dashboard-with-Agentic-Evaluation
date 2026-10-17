CREATE DATABASE IF NOT EXISTS scorecard_demo CHARACTER SET utf8mb4;
USE scorecard_demo;
SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS opportunities;
DROP TABLE IF EXISTS contacts;
DROP TABLE IF EXISTS accounts;
CREATE TABLE accounts (
  id INT PRIMARY KEY, company VARCHAR(120), industry VARCHAR(80), country_code VARCHAR(8),
  annual_revenue DECIMAL(16,2), website VARCHAR(200)) ENGINE=InnoDB;
CREATE TABLE contacts (
  id INT PRIMARY KEY, account_id INT, first_name VARCHAR(80), last_name VARCHAR(80),
  email VARCHAR(160), phone VARCHAR(40), title VARCHAR(80),
  FOREIGN KEY (account_id) REFERENCES accounts(id)) ENGINE=InnoDB;
CREATE TABLE opportunities (
  id INT PRIMARY KEY, account_id INT, contact_id INT, stage VARCHAR(40),
  amount DECIMAL(16,2), close_date VARCHAR(20),
  FOREIGN KEY (account_id) REFERENCES accounts(id),
  FOREIGN KEY (contact_id) REFERENCES contacts(id)) ENGINE=InnoDB;
INSERT INTO accounts (id, company, industry, country_code, annual_revenue, website) VALUES
  ('1', 'Acme Corp', 'Manufacturing', 'US', '12500000', 'https://acme.com'),
  ('2', 'Globex Inc', 'Technology', 'US', '48000000', 'https://globex.com'),
  ('3', 'Initech', 'Technology', 'US', '9200000', 'https://initech.com'),
  ('4', 'Bletchley Systems', 'Technology', 'GB', '15400000', 'https://bletchley.co.uk'),
  ('5', 'Sunrise Bakery', 'Technology', 'US', '640000', 'https://sunrisebakery.com'),
  ('6', 'Northwind Traders', 'Retail', 'CA', '21000000', 'https://northwind.ca'),
  ('7', 'Contoso Health', 'Healthcare', 'US', '33000000', 'https://contoso-health.com'),
  ('8', 'Umbrella Logistics', 'Logistics', 'DE', '27500000', 'https://umbrella-log.de'),
  ('9', 'Stark Finance', 'Finance', 'US', '88000000', 'https://starkfinance.com'),
  ('10', 'Wayne Holdings', 'asdfghjkl', 'US', '120000000', 'https://wayne.com'),
  ('11', 'Hooli', 'Technology', 'US', '-5000', 'https://hooli.com'),
  ('12', 'Pied Piper', 'Technology', 'US', '2100000', NULL);
INSERT INTO contacts (id, account_id, first_name, last_name, email, phone, title) VALUES
  ('1', '1', 'Wile', 'Coyote', 'wile@acme.com', '+1-555-0101', 'VP Operations'),
  ('2', '1', 'Road', 'Runner', 'road@acme.com', '+1-555-0102', 'Buyer'),
  ('3', '2', 'Hank', 'Scorpio', 'hank@globex.com', '+1-555-0103', 'CEO'),
  ('4', '2', 'Homer', 'Simpson', 'homer@globex.com', '+1-555-0104', 'Safety Inspector'),
  ('5', '3', 'Peter', 'Gibbons', 'peter@initech.com', '+1-555-0105', 'Engineer'),
  ('6', '3', 'Bill', 'Lumbergh', 'bill@initech.com', '+1-555-0106', 'VP'),
  ('7', '4', 'Alan', 'Turing', 'alan@bletchley.co.uk', '+44-20-5550107', 'Chief Scientist'),
  ('8', '5', 'Rosa', 'Diaz', 'rosa-at-sunrisebakery.com', '+1-555-0108', 'Owner'),
  ('9', '6', 'Jean', 'Tremblay', 'jean@northwind.ca', '+1-555-0109', 'Buyer'),
  ('10', '7', 'Greg', 'House', 'greg@contoso-health.com', NULL, 'Director'),
  ('11', '8', 'Hans', 'Gruber', 'hans@umbrella-log.de', '+49-30-5550110', 'Manager'),
  ('12', '9', 'Tony', 'Stark', 'tony@starkfinance.com', '+1-555-0111', 'CFO'),
  ('13', '10', 'Bruce', 'Wayne', 'bruce@wayne.com', '+1-555-0112', 'Chairman'),
  ('14', '11', 'Gavin', 'Belson', 'gavin@hooli.com', '+1-555-0113', 'CEO'),
  ('15', '12', 'Richard', 'Hendricks', 'richard@piedpiper.com', '+1-555-0114', 'Founder'),
  ('16', '99', 'Dana', 'Orphan', 'dana@nowhere.io', '+1-555-0115', 'Analyst'),
  ('17', '150', 'Evan', 'Orphan', 'evan@nowhere.io', '+1-555-0116', 'Analyst'),
  ('18', '3', 'Milton', 'Waddams', 'peter@initech.com', '+1-555-0117', 'Collections'),
  ('19', '2', 'Test', 'Test', 'test@test.com', '000-000-0000', 'TEST');
INSERT INTO opportunities (id, account_id, contact_id, stage, amount, close_date) VALUES
  ('1', '1', '1', 'Closed Won', '45000', '2026-03-14'),
  ('2', '1', '2', 'Negotiation', '12000', '2026-11-30'),
  ('3', '2', '3', 'Closed Won', '250000', '2026-02-02'),
  ('4', '2', '4', 'Proposal', '18000', '2026-12-15'),
  ('5', '3', '5', 'Closed Lost', '7000', '2026-01-20'),
  ('6', '3', '6', 'Negotiation', '32000', '2026-11-05'),
  ('7', '4', '7', 'Closed Won', '98000', '2026-04-18'),
  ('8', '5', '8', 'Proposal', '5000000', '2026-10-31'),
  ('9', '6', '9', 'Negotiation', '26000', '2026-12-01'),
  ('10', '7', '10', 'Closed Won', '61000', '2026-05-22'),
  ('11', '8', '11', 'Proposal', '15500', '2026-11-20'),
  ('12', '9', '12', 'Closed Won', '430000', '2026-06-30'),
  ('13', '10', '13', 'Negotiation', '75000', '2026-12-31'),
  ('14', '11', '14', 'Closed Lost', '-1200', '2026-03-03'),
  ('15', '12', '15', 'Proposal', '9000', NULL),
  ('16', '2', '999', 'Negotiation', '22000', '2026-11-12'),
  ('17', '77', '3', 'Proposal', '14000', '2026-12-05'),
  ('18', '1', '1', 'Closed Won', '31000', '03/14/2026');
SET FOREIGN_KEY_CHECKS = 1;
CREATE USER IF NOT EXISTS 'scorecard_ro'@'%' IDENTIFIED BY 'readonly_demo_pw';
CREATE USER IF NOT EXISTS 'scorecard_ro'@'localhost' IDENTIFIED BY 'readonly_demo_pw';
GRANT SELECT ON scorecard_demo.* TO 'scorecard_ro'@'%';
GRANT SELECT ON scorecard_demo.* TO 'scorecard_ro'@'localhost';
FLUSH PRIVILEGES;
